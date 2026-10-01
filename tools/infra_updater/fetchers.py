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
Upstream fetchers: one small function per source type that returns release candidates.

Fetchers only retrieve data. Choosing a release is done once, for every source, by
selector.ReleaseSelector. Adding a source type means writing one function:

    @register("my_source")
    def fetch_my_source(pkg, ctx) -> list[dict]:
        ...

A candidate is a dict:
    version     version string as published upstream (required)
    url         artifact URL, or the release page when there is no single artifact
    notes       release notes text (optional)
    prerelease  True if upstream flags it as a pre-release (optional)
    sha256      artifact checksum (optional)

Per-package options come from pkg["source_options"] (e.g. asset_pattern, apt_package, arch).
"""

import gzip
import json
import re
import threading
from typing import Any, Callable, Dict, List, Optional

from pydantic import BaseModel, Field

import http_client
from http_client import GitHubClient
from prompts import render_prompt

Candidate = Dict[str, Any]
FetchFn = Callable[[Dict[str, Any], "FetchContext"], List[Candidate]]
ResolveFn = Callable[[Dict[str, Any], Candidate, "FetchContext"], Candidate]

_FETCHERS: Dict[str, FetchFn] = {}
_RESOLVERS: Dict[str, ResolveFn] = {}

VERSION_IN_TEXT_RE = re.compile(r"\d+(?:\.\d+)+")
LEADING_VERSION_RE = re.compile(r"^v?\d+(?:\.\d+)*")
ARTIFACT_LINK_RE = re.compile(r"https?://[^\s\"'<>\\&]+?\.(?:run|sh|deb|rpm|tgz|tar\.gz|tar\.bz2|zip)\b")


def register(source_type: str, resolve: Optional[ResolveFn] = None):
    """Registers a fetcher (and optionally a resolver that fills in the selected candidate's artifact)."""
    def deco(fn: FetchFn) -> FetchFn:
        _FETCHERS[source_type] = fn
        if resolve:
            _RESOLVERS[source_type] = resolve
        return fn
    return deco


def source_types() -> List[str]:
    return sorted(_FETCHERS)


class FetchContext:
    """Shared, thread-safe resources for one qualification run (LLM, GitHub client, page cache)."""

    def __init__(self, llm=None, github_token: Optional[str] = None):
        self.llm = llm
        self.github = GitHubClient(github_token)
        self._pages: Dict[str, str] = {}
        self._lock = threading.Lock()

    def get_text(self, url: str) -> str:
        """GETs a page once per run (several packages may share a source page)."""
        with self._lock:
            if url in self._pages:
                return self._pages[url]
        resp = http_client.get(url)
        resp.raise_for_status()
        with self._lock:
            self._pages[url] = resp.text
        return resp.text


def fetch_candidates(pkg: Dict[str, Any], ctx: FetchContext) -> List[Candidate]:
    source_type = pkg.get("upstream_type")
    fn = _FETCHERS.get(source_type)
    if not fn:
        raise ValueError(f"Unsupported upstream_type '{source_type}' (known: {', '.join(source_types())})")
    return fn(pkg, ctx)


def resolve_candidate(pkg: Dict[str, Any], cand: Candidate, ctx: FetchContext) -> Candidate:
    """Fills in artifact details for the selected candidate, for sources that need a second lookup."""
    fn = _RESOLVERS.get(pkg.get("upstream_type"))
    return fn(pkg, cand, ctx) if fn else cand


def _opts(pkg: Dict[str, Any]) -> Dict[str, Any]:
    return pkg.get("source_options") or {}


def _require(pkg: Dict[str, Any], *keys: str) -> List[Any]:
    opts = _opts(pkg)
    missing = [k for k in keys if not opts.get(k)]
    if missing:
        raise ValueError(f"{pkg['package_id']}: source_options missing {missing} for '{pkg.get('upstream_type')}'")
    return [opts[k] for k in keys]


# ---------------------------------------------------------------- github_release

def _github_repo(url: str) -> str:
    m = re.search(r"github\.com/([^/\s]+/[^/\s#?]+)", url or "")
    if not m:
        raise ValueError(f"Not a GitHub repository URL: {url}")
    return m.group(1).removesuffix(".git").rstrip("/")


@register("github_release")
def fetch_github_release(pkg: Dict[str, Any], ctx: FetchContext) -> List[Candidate]:
    """Releases (falling back to tags for repos that publish none). With source_options.asset_pattern,
    each candidate's url is the matching release asset and releases without one are skipped."""
    repo = _github_repo(pkg.get("source_url"))
    per_page = int(_opts(pkg).get("per_page", 30))
    asset_re = re.compile(_opts(pkg)["asset_pattern"]) if _opts(pkg).get("asset_pattern") else None

    releases = ctx.github.get(f"/repos/{repo}/releases", params={"per_page": per_page})
    if releases:
        out = []
        for r in releases:
            if r.get("draft"):
                continue
            url = r.get("html_url")
            if asset_re:
                asset = next((a for a in r.get("assets", []) if asset_re.search(a.get("name", ""))), None)
                if not asset:
                    continue
                url = asset.get("browser_download_url")
            out.append({"version": r.get("tag_name", ""), "url": url, "notes": r.get("body") or "",
                        "prerelease": bool(r.get("prerelease")), "published": r.get("published_at")})
        return out

    tags = ctx.github.get(f"/repos/{repo}/tags", params={"per_page": per_page})
    return [{"version": t.get("name", ""), "url": f"https://github.com/{repo}/releases/tag/{t.get('name', '')}"}
            for t in tags]


# ---------------------------------------------------------------- apt_repository

@register("apt_repository")
def fetch_apt_repository(pkg: Dict[str, Any], ctx: FetchContext) -> List[Candidate]:
    """All versions of source_options.apt_package in <source_url>/<distro>/<arch>/Packages.gz."""
    apt_package, distro, arch = _require(pkg, "apt_package", "distro", "arch")
    repo_base = f"{pkg['source_url'].rstrip('/')}/{distro}/{arch}"
    resp = http_client.get(f"{repo_base}/Packages.gz")
    resp.raise_for_status()
    raw = resp.content
    # Packages.gz is normally served as-is; some mirrors send Content-Encoding: gzip (already decoded).
    text = (gzip.decompress(raw) if raw[:2] == b"\x1f\x8b" else raw).decode("utf-8", errors="ignore")

    out = []
    for block in text.split("\n\n"):
        fields = dict(re.findall(r"^([A-Za-z0-9-]+):\s*(.*)$", block, flags=re.MULTILINE))
        if fields.get("Package") != apt_package or not fields.get("Version"):
            continue
        filename = fields.get("Filename", "").lstrip("./")
        out.append({"version": fields["Version"], "url": f"{repo_base}/{filename}" if filename else repo_base,
                    "sha256": fields.get("SHA256")})
    if not out:
        raise RuntimeError(f"Package '{apt_package}' not found in {repo_base}/Packages.gz")
    return out


# ---------------------------------------------------------------- docker_hub

def split_tag(tag: str) -> tuple:
    """'13.0.0-base-ubuntu24.04' -> ('13.0.0', 'base-ubuntu24.04'); 'v1.2' -> ('v1.2', '')."""
    m = LEADING_VERSION_RE.match(tag or "")
    core = m.group(0) if m else ""
    return core, (tag[len(core):].lstrip("-") if core else tag)


def _docker_repo(source_url: str) -> str:
    m = re.search(r"(?:repositories|r)/([^/]+)/([^/?]+)", source_url or "")
    if m:
        return f"{m.group(1)}/{m.group(2)}"
    if "/" in source_url and not source_url.startswith("http"):
        return source_url
    return f"library/{source_url.rstrip('/').split('/')[-1]}"


@register("docker_hub")
def fetch_docker_hub(pkg: Dict[str, Any], ctx: FetchContext) -> List[Candidate]:
    """Tags of the image that share the deployed tag's flavor (e.g. '-base-ubuntu24.04')."""
    repo = _docker_repo(pkg.get("source_url", ""))
    _, flavor = split_tag(pkg.get("current_version", ""))
    params = {"page_size": 100, **({"name": flavor} if flavor else {})}
    resp = http_client.get(f"https://hub.docker.com/v2/repositories/{repo}/tags", params=params)
    resp.raise_for_status()
    out = []
    for t in resp.json().get("results", []):
        name = t.get("name", "")
        if flavor and split_tag(name)[1] != flavor:
            continue
        out.append({"version": name, "url": f"https://hub.docker.com/v2/repositories/{repo}/tags/{name}",
                    "published": t.get("last_updated")})
    return out


def docker_tag_exists(repo: str, tag: str) -> bool:
    try:
        return http_client.get(f"https://hub.docker.com/v2/repositories/{repo}/tags/{tag}").status_code == 200
    except http_client.requests.RequestException:
        return False


def variant_image_ref(image_ref: str, new_version: str) -> Optional[str]:
    """
    Moves an image reference to new_version while keeping its own variant, e.g.
    nvidia/cuda:11.0.3-runtime-ubuntu20.04 + 13.4.2-base-ubuntu24.04
      -> nvidia/cuda:13.4.2-runtime-ubuntu20.04 if it exists, else nvidia/cuda:13.4.2-runtime-ubuntu24.04.
    Docker Hub tags are checked for existence; returns None if no matching variant exists.
    """
    repo, _, old_tag = image_ref.rpartition(":")
    new_core, new_flavor = split_tag(new_version)
    old_core, old_flavor = split_tag(old_tag)
    if not new_core or not old_core:
        return None
    if old_core.startswith("v") != new_core.startswith("v"):
        new_core = f"v{new_core}" if old_core.startswith("v") else new_core.lstrip("v")
    if not old_flavor:
        return f"{repo}:{new_core}"

    tags = [f"{new_core}-{old_flavor}"]
    kind, _, _old_os = old_flavor.partition("-")
    _, _, new_os = new_flavor.partition("-")
    if new_os and _old_os:
        tags.append(f"{new_core}-{kind}-{new_os}")

    first = repo.split("/")[0]
    on_docker_hub = "." not in first and ":" not in first
    if not on_docker_hub:
        return f"{repo}:{tags[0]}"
    hub_repo = repo if "/" in repo else f"library/{repo}"
    return next((f"{repo}:{t}" for t in dict.fromkeys(tags) if docker_tag_exists(hub_repo, t)), None)


# ---------------------------------------------------------------- archive_scraper (HTML download page)

def _version_from_url(url: str) -> Optional[str]:
    """Prefers a pure version path segment (/compute/cuda/13.4.2/...), else the first version in the filename."""
    path = url.split("?", 1)[0]
    for seg in path.split("/")[3:-1]:
        if re.fullmatch(r"\d+(?:\.\d+)+", seg):
            return seg
    m = VERSION_IN_TEXT_RE.search(path.rsplit("/", 1)[-1])
    return m.group(0) if m else None


@register("archive_scraper")
def fetch_archive_page(pkg: Dict[str, Any], ctx: FetchContext) -> List[Candidate]:
    """Artifact links on a downloads page, filtered by source_options.asset_pattern."""
    html = ctx.get_text(pkg["source_url"])
    asset_re = re.compile(_opts(pkg)["asset_pattern"]) if _opts(pkg).get("asset_pattern") else None
    out, seen = [], set()
    for url in ARTIFACT_LINK_RE.findall(html):
        if url in seen or (asset_re and not asset_re.search(url)):
            continue
        seen.add(url)
        version = _version_from_url(url)
        if version:
            out.append({"version": version, "url": url})
    return out


# ---------------------------------------------------------------- raw_manifest

class ManifestVersion(BaseModel):
    version: str = Field(description="The package's image tag / version exactly as written in the manifest")
    reasoning: str = Field(description="Which image line the version was read from")


IMAGE_TAG_RE = re.compile(r"image:\s*[\"']?[^\s\"':]+(?::\d+)?/[^\s\"']*?:(v?\d+(?:\.\d+)+)")


@register("raw_manifest")
def fetch_manifest(pkg: Dict[str, Any], ctx: FetchContext) -> List[Candidate]:
    """
    The version currently published in a manifest. The LLM reads the package's own image tag; without
    an LLM (or if it fails) every image tag written in the deployed version's style (v-prefix or not)
    is returned and the selector picks the newest.
    """
    url = pkg["source_url"]
    content = ctx.get_text(url)
    if ctx.llm:
        try:
            res = ctx.llm.generate_json(render_prompt(
                "extract_manifest_version", package_id=pkg["package_id"], current_version=pkg.get("current_version", "-"),
                manifest_url=url, manifest_content=content[:6000], extra_rules=_opts(pkg).get("prompt_hints"),
            ), ManifestVersion)
            return [{"version": res.version.strip(), "url": url, "notes": res.reasoning, "verified": True}]
        except Exception as ex:  # pylint: disable=broad-except
            print(f"[WARN] {pkg['package_id']}: manifest LLM extraction failed ({ex}); using image tags.")
    v_style = (pkg.get("current_version") or "").startswith("v")
    return [{"version": t, "url": url} for t in sorted(set(IMAGE_TAG_RE.findall(content))) if t.startswith("v") == v_style]


# ---------------------------------------------------------------- mft_api

MFT_API_URL = "https://downloaders.azurewebsites.net/downloaders/mft_downloader/helper.php"


def _resolve_mft(pkg: Dict[str, Any], cand: Candidate, ctx: FetchContext) -> Candidate:
    """Looks up the artifact URL and SHA256 for the selected MFT version."""
    (arch,) = _require(pkg, "arch")
    info = http_client.post(MFT_API_URL, data={
        "action": "get_download_info", "version": cand["version"],
        "distro": "Linux", "os": _opts(pkg).get("os", "DEB based"), "arch": arch,
    }).json()
    files = info.get("files") or []
    if not files:
        raise RuntimeError(f"MFT downloader returned no files for {cand['version']} ({arch})")
    return {**cand, "url": files[0].get("url"), "sha256": files[0].get("sha")}


@register("mft_api", resolve=_resolve_mft)
def fetch_mft(pkg: Dict[str, Any], ctx: FetchContext) -> List[Candidate]:
    """Versions from NVIDIA's MFT downloader API (GA list + latest)."""
    data = http_client.post(MFT_API_URL, data={"action": "get_versions"}).json()
    entries = ([data["latest"]] if data.get("latest") else []) + list(data.get("ga") or [])
    out, seen = [], set()
    for entry in entries:
        version, _, label = str(entry).partition(" ")
        if version not in seen:
            seen.add(version)
            out.append({"version": version, "url": None, "notes": label.strip()})
    return out


def describe(cands: List[Candidate], limit: int = 25) -> str:
    """Compact JSON listing of candidates for prompts."""
    rows = [{k: v for k, v in {
        "version": c.get("version"),
        "prerelease": c.get("prerelease") or None,
        "published": c.get("published"),
        "artifact": (c.get("url") or "").rsplit("/", 1)[-1] or None,
        "notes": (c.get("notes") or "")[:160].replace("\n", " ") or None,
    }.items() if v} for c in cands[:limit]]
    return json.dumps(rows, indent=1)
