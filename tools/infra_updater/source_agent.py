#!/usr/bin/env python3
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
Source Qualification Agent for Cluster Toolkit Automated Dependency Management.
Aligned with Section 4.2 of the Implementation Guide:
  1. Checks official archives and repositories.
  2. Discards non-production releases (RCs, betas, alphas, developer previews).
  3. Executes upfront learned rule enforcement with multi-blueprint scoping.
  4. Performs artifact liveness verification.
"""

import datetime
import gzip
import json
import os
import re
import sys
import uuid
from typing import Dict, List, Optional, Tuple, Any
import requests
from packaging.specifiers import SpecifierSet
from pydantic import BaseModel, Field

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
if BASE_DIR not in sys.path:
    sys.path.insert(0, BASE_DIR)
from config import get_config
from datastore import BaseDataStore as DataStore, get_datastore
import http_client
from http_client import GitHubClient
from llm_client import LLMClient, clean_error_message, get_llm_client
import policy
from statuses import POLICY_STATUSES, PR_TRACKED_CANDIDATE_STATUSES, CandidateStatus, PackageStatus
from versions import parse_semver
from prompts import (
    get_archive_scraper_prompt,
    get_manifest_regex_prompt,
    get_github_release_prompt,
    get_apt_repo_prompt,
    get_docker_hub_prompt,
    get_release_summary_prompt,
)

CONFIG = get_config()

# ==============================================================================
# Pydantic Structured Contracts for LLM Function Calling / Structured Output
# ==============================================================================

class CandidateReleaseExtraction(BaseModel):
    version: str = Field(description="Normalized semantic version of the release, e.g., '13.4.2' or '1.4.11'")
    download_url: str = Field(description="Direct HTTP/HTTPS download URL for the target installer artifact or repository")
    filename: str = Field(description="Filename of the installer artifact, e.g. 'cuda_13.4.2_linux_sbsa.run' or 'gve-1.4.11.deb'")
    is_production_ga: bool = Field(description="True if this is a production-stable General Availability (GA) release, False if preview, RC, or beta")
    release_channel: str = Field(description="Release channel name, e.g. 'production-stable', 'developer-preview', 'release-candidate'")
    target_architecture: Optional[str] = Field(default=None, description="Architecture name if applicable, e.g. 'arm64-sbsa' or 'x86_64'")
    reasoning: str = Field(description="Brief explanation of why this version and artifact were chosen as the latest stable GA release")


# ==============================================================================
# Upfront Rule Checker (Section 4.2 & Case 3 Multi-Blueprint Scoping)
# ==============================================================================

class UpfrontRuleChecker:
    """Evaluates candidate versions against learned rules with multi-blueprint scoping."""

    def __init__(self, store: Optional[DataStore] = None):
        self.store = store or get_datastore()

    def check_version(
        self, package_id: str, candidate_version_str: str, blueprint_path: Optional[str] = None
    ) -> Tuple[bool, Optional[Dict[str, Any]]]:
        """
        Checks if candidate_version_str is blocked by any learned rules for package_id.
        Supports multi-blueprint isolation by checking rule scope.
        Returns: (is_blocked, matched_rule_dict)
        """
        cand_ver = parse_semver(candidate_version_str)
        rules = self.store.list_rules(package_id)

        matched_rule = None
        for rule in rules:
            action = rule.get("action", "BLOCK")
            if action != "BLOCK":
                continue

            # Check blueprint scope if specified (Section 5, Case 3)
            scope = rule.get("scope", {})
            if isinstance(scope, str):
                try:
                    scope = json.loads(scope)
                except Exception:
                    scope = {}
            if blueprint_path and scope:
                target_bp = scope.get("blueprint", "*")
                if target_bp != "*" and target_bp != blueprint_path:
                    continue

            constraint = rule.get("version_constraint", "")
            if cand_ver:
                try:
                    spec = SpecifierSet(constraint)
                    if cand_ver in spec:
                        matched_rule = rule
                        break
                except Exception:
                    pass

            raw_target = constraint.replace("==", "").replace("=", "").strip()
            if raw_target in candidate_version_str:
                matched_rule = rule
                break

        return (matched_rule is not None, matched_rule)


# ==============================================================================
# Upstream Providers (Clean, Data-Driven)
# ==============================================================================

class ArchiveScraperProvider:
    """Discovers latest GA release using Gemini LLM extraction over upstream release pages."""
    _cached_results = {}

    def __init__(self, llm: Optional[LLMClient] = None):
        self.llm = llm

    def _extract_with_llm(self, package_id: str, page_url: str, html_content: str, target_arch: str) -> Optional[Dict[str, Any]]:
        if not self.llm:
            return None

        all_urls = set(re.findall(r"https?://[^\s\"<>\\&]+?\.(?:run|tgz|deb|rpm|tar\.gz|zip)", html_content))
        cuda_urls = set(re.findall(r"https://developer\.download\.nvidia\.com/compute/cuda/[^\s\"<>\\&]+", html_content))
        all_urls.update(cuda_urls)

        if not all_urls:
            return None

        if "arm64" in target_arch or "sbsa" in target_arch or "aarch64" in target_arch:
            sample_urls = [u for u in all_urls if "linux" in u.lower() or "sbsa" in u.lower() or "aarch64" in u.lower() or "arm64" in u.lower()]
        elif "x86" in target_arch or "amd64" in target_arch:
            sample_urls = [u for u in all_urls if ("linux" in u.lower() or "x86" in u.lower() or "amd64" in u.lower()) and "sbsa" not in u.lower() and "arm" not in u.lower() and "aarch" not in u.lower()]
        else:
            sample_urls = list(all_urls)

        if not sample_urls:
            sample_urls = list(all_urls)[:30]

        prompt = get_archive_scraper_prompt(
            page_url=page_url,
            package_id=package_id,
            target_arch=target_arch,
            discovered_urls_json=json.dumps(sample_urls, indent=2)
        )
        try:
            cand = self.llm.generate_json(prompt, CandidateReleaseExtraction).model_dump()
            dl_url = cand.get("download_url")
            version = cand.get("version")
            filename = cand.get("filename") or (os.path.basename(dl_url) if dl_url else "")

            # Return candidate release metadata directly for qualification gate processing
            if dl_url and version:
                return {
                    "version": version,
                    "download_url": dl_url,
                    "filename": filename,
                    "channel": cand.get("release_channel", "production-stable"),
                    "release_notes": f"Upstream release {version} (LLM-verified GA for {target_arch}): {cand.get('reasoning', '')}",
                    "tag_name": f"{package_id}_{version}",
                    "prerelease": not cand.get("is_production_ga", True),
                    "draft": False
                }
        except Exception as lex:
            print(f"[WARN] LLM candidate extraction failed: {clean_error_message(lex)}")

        return None

    def get_latest_candidate(
        self,
        package_id: str,
        source_url: str
    ) -> Optional[Dict[str, Any]]:
        cache_key = f"{package_id}_{source_url}"
        if cache_key in self._cached_results:
            return self._cached_results[cache_key]

        if not self.llm:
            raise RuntimeError(f"Gemini LLM client is required for ArchiveScraperProvider on {package_id}")

        target_arch = "arm64-sbsa" if "arm64" in package_id else "x86_64"

        urls_to_try = []
        if "nvidia.com" in source_url:
            urls_to_try.extend([
                "https://developer.nvidia.com/cuda-downloads",
                "https://developer.nvidia.com/cuda-toolkit-archive"
            ])
        else:
            urls_to_try.append(source_url)

        for u in urls_to_try:
            try:
                r = http_client.get(u)
                if r.status_code == 200:
                    cand = self._extract_with_llm(package_id, u, r.text, target_arch)
                    if cand:
                        self._cached_results[cache_key] = cand
                        return cand
            except Exception as ex:
                print(f"[WARN] Error fetching {u}: {ex}")

        raise RuntimeError(f"LLM could not discover or qualify upstream GA release for {package_id} from {source_url}")


class ManifestRegexProvider:
    """Discovers upstream version from raw Kubernetes/container manifests via Gemini LLM extraction."""

    def __init__(self, llm: Optional[LLMClient] = None):
        self.llm = llm

    def _extract_with_llm(self, package_id: str, manifest_url: str, content: str) -> Optional[Dict[str, Any]]:
        if not self.llm:
            return None

        prompt = get_manifest_regex_prompt(
            package_id=package_id,
            manifest_url=manifest_url,
            manifest_content=content[:2500]
        )
        try:
            extracted = self.llm.generate_json(prompt, CandidateReleaseExtraction)
            if not extracted.version:
                return None

            return {
                "version": extracted.version,
                "download_url": manifest_url,
                "filename": os.path.basename(manifest_url),
                "channel": "production-stable",
                "release_notes": extracted.reasoning or f"Upstream production release {extracted.version} extracted from {manifest_url}.",
                "tag_name": extracted.version,
                "prerelease": False,
                "draft": False
            }
        except Exception as e:
            clean_msg = clean_error_message(e)
            print(f"[WARN] ManifestRegexProvider LLM extraction error for {package_id}: {clean_msg}")
            raise RuntimeError(f"LLM extraction failed for manifest {manifest_url} on {package_id}: {clean_msg}")

    def get_candidate(self, package_id: str, manifest_url: str, current_version: Optional[str] = None) -> Optional[Dict[str, Any]]:
        if not self.llm:
            raise RuntimeError(f"Gemini LLM client is required for ManifestProvider on {package_id}")

        resp = http_client.get(manifest_url)
        if resp.status_code != 200:
            raise RuntimeError(f"Failed to fetch manifest from {manifest_url} (HTTP {resp.status_code})")
        content = resp.text

        llm_cand = self._extract_with_llm(package_id, manifest_url, content)
        if not llm_cand:
            raise RuntimeError(f"LLM could not discover or qualify upstream GA release for {package_id} from manifest {manifest_url}")

        if current_version:
            v = llm_cand.get("version", "")
            if current_version.startswith("v") and not v.startswith("v"):
                llm_cand["version"] = f"v{v}"
            elif not current_version.startswith("v") and v.startswith("v"):
                llm_cand["version"] = v.lstrip("v")

        return llm_cand


class GitHubReleaseProvider:
    """Discovers latest GA release via Gemini LLM extraction over upstream releases/tags."""

    def __init__(self, llm: Optional[LLMClient] = None):
        self.llm = llm

    @staticmethod
    def extract_repo_from_url(url: str) -> Optional[str]:
        m = re.search(r"github\.com/([^/]+/[^/\s]+)", url)
        return m.group(1).rstrip("/") if m else None

    def _extract_with_llm(
        self, package_id: str, source_url: str, releases: List[Dict[str, Any]], current_version: Optional[str] = None
    ) -> Optional[Dict[str, Any]]:
        if not self.llm:
            return None

        candidates_summary = []
        for rel in releases[:15]:
            tag = rel.get("tag_name", "")
            assets = [{"name": a.get("name"), "url": a.get("browser_download_url")} for a in rel.get("assets", [])]
            candidates_summary.append({
                "tag": tag,
                "is_prerelease": rel.get("prerelease", False),
                "is_draft": rel.get("draft", False),
                "release_url": rel.get("html_url", ""),
                "assets": assets,
                "notes_snippet": (rel.get("body") or "")[:200].replace("\n", " ")
            })

        prompt = get_github_release_prompt(
            source_url=source_url,
            package_id=package_id,
            candidates_summary_json=json.dumps(candidates_summary, indent=2),
            current_version=current_version or "-"
        )
        try:
            extracted = self.llm.generate_json(prompt, CandidateReleaseExtraction)
            if not extracted.is_production_ga or not extracted.version:
                return None

            clean_ver = extracted.version.lstrip("v")
            curr_semver = parse_semver(current_version) if current_version else None
            ext_semver = parse_semver(clean_ver)

            # If extracted version is strictly lower than deployed version (e.g. repo changed from CalVer 25.x to SemVer 0.x),
            # maintain continuity within the active major track if releases exist in that track.
            if curr_semver and ext_semver and ext_semver < curr_semver:
                curr_major = str(curr_semver.major)
                track_releases = []
                for r in releases:
                    if r.get("prerelease") or r.get("draft"):
                        continue
                    t_tag = r.get("tag_name", "").lstrip("v")
                    t_semver = parse_semver(t_tag)
                    if t_semver and str(t_semver.major) == curr_major:
                        track_releases.append((t_semver, r))
                if track_releases:
                    track_releases.sort(key=lambda x: x[0], reverse=True)
                    best_semver, best_rel = track_releases[0]
                    clean_ver = str(best_semver)
                    has_v = (current_version and current_version.startswith("v")) or best_rel.get("tag_name", "").startswith("v")
                    target_ver = f"v{clean_ver}" if has_v else clean_ver
                    release_notes = best_rel.get("body", "") or f"Upstream GA release {target_ver} (track {curr_major}.x)."
                    return {
                        "version": target_ver,
                        "download_url": best_rel.get("html_url", source_url),
                        "filename": f"{package_id}-{target_ver}.tar.gz",
                        "channel": "production-stable",
                        "release_notes": release_notes,
                        "tag_name": best_rel.get("tag_name", target_ver),
                        "prerelease": False,
                        "draft": False
                    }

            matching_rel = next((r for r in releases if extracted.version in r.get("tag_name", "") or clean_ver in r.get("tag_name", "")), None)
            has_v = (current_version and current_version.startswith("v")) or (matching_rel and matching_rel.get("tag_name", "").startswith("v")) or extracted.version.startswith("v")
            target_ver = f"v{clean_ver}" if has_v else clean_ver
            release_notes = matching_rel.get("body", "") if matching_rel else (extracted.reasoning or f"Upstream GA release {target_ver}.")

            dl_url = extracted.download_url
            dl_filename = extracted.filename or f"{package_id}-{target_ver}.tar.gz"

            if package_id == "cmake":
                parts = clean_ver.split(".")
                maj_min = f"v{parts[0]}.{parts[1]}" if len(parts) >= 2 else f"v{clean_ver}"
                sh_asset = None
                if matching_rel:
                    sh_asset = next((a for a in matching_rel.get("assets", []) if a.get("name", "").endswith("-linux-x86_64.sh")), None)
                if sh_asset:
                    dl_url = sh_asset.get("browser_download_url") or sh_asset.get("url")
                    dl_filename = sh_asset.get("name")
                else:
                    dl_url = f"https://cmake.org/files/{maj_min}/cmake-{clean_ver}-linux-x86_64.sh"
                    dl_filename = f"cmake-{clean_ver}-linux-x86_64.sh"

            return {
                "version": target_ver,
                "download_url": dl_url,
                "filename": dl_filename,
                "channel": extracted.release_channel or "production-stable",
                "release_notes": release_notes,
                "tag_name": matching_rel.get("tag_name") if matching_rel else target_ver,
                "prerelease": False,
                "draft": False
            }
        except Exception as e:
            clean_msg = clean_error_message(e)
            print(f"[WARN] GitHubReleaseProvider LLM extraction error for {package_id}: {clean_msg}")
            raise RuntimeError(f"LLM extraction failed for {package_id} from GitHub {source_url}: {clean_msg}")

    def get_latest_candidate(
        self, package_id: str, source_url: str, current_version: Optional[str] = None
    ) -> Optional[Dict[str, Any]]:
        repo = self.extract_repo_from_url(source_url)
        if not repo:
            raise ValueError(f"Could not extract GitHub repository from {source_url}")

        gh = GitHubClient(CONFIG.github_token)
        releases = gh.get(f"/repos/{repo}/releases", params={"per_page": 15})

        # Repositories that publish no GitHub releases (e.g. openmpi) are read from git tags.
        if not releases:
            tags = gh.get(f"/repos/{repo}/tags", params={"per_page": 15})
            if tags:
                releases = [
                    {
                        "tag_name": t.get("name", ""),
                        "html_url": f"https://github.com/{repo}/releases/tag/{t.get('name', '')}",
                        "body": f"Release tag {t.get('name', '')} from {repo}.",
                        "prerelease": any(x in t.get("name", "").lower() for x in ["rc", "beta", "alpha", "preview"]),
                        "draft": False,
                        "assets": []
                    }
                    for t in tags
                ]

        if not releases:
            raise RuntimeError(f"No releases or tags returned by GitHub API for {repo}")

        llm_cand = self._extract_with_llm(package_id, source_url, releases, current_version=current_version)
        if not llm_cand:
            raise RuntimeError(f"LLM could not discover or qualify upstream GA release for {package_id} from {source_url}")
        return llm_cand


class AptRepoProvider:
    """Discovers package versions dynamically from official APT package repository index (Packages.gz)."""

    def __init__(self, llm: Optional[LLMClient] = None):
        self.llm = llm
        self._cached_results = {}

    def resolve_index_url(self, source_url: str, distro: str = "debian12", arch: str = "x86_64") -> Tuple[str, str]:
        """Dynamically resolves the full Packages.gz URL and repository base without hardcoding."""
        clean_url = source_url.strip()
        if clean_url.endswith("Packages.gz") or clean_url.endswith("Packages"):
            repo_base = clean_url.rsplit("/", 3)[0] if "/repos/" in clean_url else os.path.dirname(clean_url)
            return clean_url, repo_base

        repo_base = clean_url.rstrip("/")
        if "/repos/" in repo_base and any(d in repo_base for d in ("debian", "ubuntu", "rhel", "rocky")):
            return f"{repo_base}/Packages.gz", repo_base

        return f"{repo_base}/{distro}/{arch}/Packages.gz", f"{repo_base}/{distro}/{arch}"

    def get_latest_candidate(self, package_id: str, source_url: str, distro: str = "debian12", arch: str = "x86_64") -> Optional[Dict[str, Any]]:
        cache_key = f"{package_id}_{source_url}"
        if cache_key in self._cached_results:
            return self._cached_results[cache_key]

        if not self.llm:
            raise RuntimeError(f"Gemini LLM client is required for AptRepoProvider on {package_id}")

        index_url, repo_base = self.resolve_index_url(source_url, distro=distro, arch=arch)
        resp = http_client.get(index_url)
        resp.raise_for_status()
        raw = resp.content
        # Packages.gz is normally served as-is; some mirrors send Content-Encoding: gzip (already decoded).
        data = (gzip.decompress(raw) if raw[:2] == b"\x1f\x8b" else raw).decode("utf-8", errors="ignore")

        blocks = data.split("\n\n")
        packages_in_index = set()
        block_map = {}
        for b in blocks:
            pkg_m = re.search(r"Package:\s*([^\s\n]+)", b)
            if pkg_m:
                p_name = pkg_m.group(1)
                packages_in_index.add(p_name)
                ver_m = re.search(r"Version:\s*([^\s\n]+)", b)
                if ver_m:
                    block_map.setdefault(p_name, {})[ver_m.group(1)] = b

        # Dynamic package resolution: match exact, prefix, or alias without rigid hardcoded mappings
        target_pkg = None
        if package_id in packages_in_index:
            target_pkg = package_id
        else:
            matching = [p for p in packages_in_index if package_id in p or p in package_id]
            if not matching and "dcgm" in package_id:
                matching = [p for p in packages_in_index if "datacenter-gpu-manager" in p]
            if matching:
                core_matches = [m for m in matching if "core" in m]
                target_pkg = core_matches[0] if core_matches else matching[0]
            else:
                target_pkg = package_id

        version_dict = block_map.get(target_pkg, {})
        candidate_versions = list(version_dict.keys())
        if not candidate_versions:
            raise RuntimeError(f"No package blocks found for {target_pkg} in APT repository {source_url}")

        prompt = get_apt_repo_prompt(
            package_id=package_id,
            target_pkg=target_pkg,
            repo_source=source_url,
            available_versions_json=json.dumps(sorted(set(candidate_versions)), indent=2)
        )
        try:
            extracted = self.llm.generate_json(prompt, CandidateReleaseExtraction)
            if not extracted.version:
                raise RuntimeError(f"LLM returned empty candidate version for APT package {package_id}")

            best_ver = extracted.version
            if best_ver not in version_dict:
                if f"1:{best_ver}" in version_dict:
                    best_ver = f"1:{best_ver}"
                else:
                    for k in version_dict:
                        if best_ver in k or k.split(":")[-1].startswith(best_ver):
                            best_ver = k
                            break

            matched_block = version_dict.get(best_ver, "")
            file_m = re.search(r"Filename:\s*\.?/?([^\s\n]+)", matched_block)
            deb_path = file_m.group(1) if file_m else f"{target_pkg}_{best_ver}_{arch}.deb"
            download_url = f"{repo_base}/{deb_path}" if not deb_path.startswith("http") else deb_path
            sha_m = re.search(r"SHA256:\s*([^\s\n]+)", matched_block)
            sha256 = sha_m.group(1) if sha_m else None

            result = {
                "version": best_ver,
                "download_url": download_url,
                "filename": os.path.basename(deb_path),
                "channel": extracted.release_channel or "production-stable",
                "release_notes": extracted.reasoning or f"Latest production release ({best_ver}) from APT repository.",
                "tag_name": f"{package_id}_{best_ver}",
                "checksum_sha256": sha256,
                "prerelease": not extracted.is_production_ga,
                "draft": False
            }
            self._cached_results[cache_key] = result
            return result
        except Exception as ex:
            clean_msg = clean_error_message(ex)
            print(f"[WARN] AptRepoProvider LLM extraction error: {clean_msg}")
            raise RuntimeError(f"LLM extraction failed for {package_id} from APT repo {source_url}: {clean_msg}")


class MftProvider(ArchiveScraperProvider):
    """Mellanox Firmware Tools (MFT) provider using generalized archive discovery with fallback to official downloader."""

    API_URL = "https://downloaders.azurewebsites.net/downloaders/mft_downloader/helper.php"

    def get_latest_candidate(self, package_id: str = "mft", arch: str = "aarch64", source_url: str = "") -> Optional[Dict[str, Any]]:
        # 1. Attempt generalized archive discovery if source_url is an archive directory
        if source_url and ("mellanox" in source_url or "MFT" in source_url) and not source_url.endswith(".php"):
            try:
                cand = super().get_latest_candidate(package_id, source_url)
                if cand:
                    return cand
            except Exception:
                pass

        # 2. Fallback to official downloader API endpoint
        try:
            data = http_client.post(self.API_URL, data={"action": "get_versions"}).json()

            latest_version = data.get("latest")
            if not latest_version and data.get("ga"):
                latest_version = data["ga"][0]
            if not latest_version:
                return None

            info = http_client.post(self.API_URL, data={
                "action": "get_download_info",
                "version": latest_version,
                "distro": "Linux",
                "os": "DEB based",
                "arch": arch
            }).json()

            files = info.get("files", [])
            if not files:
                return None

            file_info = files[0]
            download_url = file_info.get("url")
            filename = file_info.get("file", f"mft-{latest_version}-{arch}-deb.tgz")
            sha256 = file_info.get("sha")

            return {
                "version": latest_version,
                "download_url": download_url,
                "filename": filename,
                "channel": "production-stable",
                "release_notes": f"https://networking-docs.nvidia.com/mftswum/{latest_version.split('-')[0]}/release-notes",
                "tag_name": f"mft-{latest_version}",
                "checksum_sha256": sha256,
                "prerelease": False,
                "draft": False
            }
        except Exception as ex:
            print(f"[WARN] MftProvider error: {clean_error_message(ex)}")
            return None


class DockerHubProvider:
    """Discovers upstream container image tags via Docker Hub API with Gemini LLM qualification."""

    def __init__(self, llm: Optional[LLMClient] = None):
        self.llm = llm

    @staticmethod
    def parse_repo_and_namespace(source_url: str) -> Tuple[str, str]:
        """Extracts namespace and repository from Docker Hub source URL or image name."""
        clean = (source_url or "").strip().rstrip("/")
        m = re.search(r"(?:repositories|r)/([^/]+)/([^/\?]+)", clean)
        if m:
            return m.group(1), m.group(2)
        if "/" in clean and not clean.startswith("http"):
            parts = clean.split("/", 1)
            return parts[0], parts[1]
        return "library", clean.split("/")[-1]

    def _extract_with_llm(self, package_id: str, repo_str: str, current_version: str, candidate_tags: List[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
        if not self.llm:
            return None

        prompt = get_docker_hub_prompt(
            package_id=package_id,
            repository=repo_str,
            current_version=current_version,
            candidate_tags_json=json.dumps(candidate_tags[:25], indent=2)
        )
        try:
            extracted = self.llm.generate_json(prompt, CandidateReleaseExtraction)
            if not extracted.is_production_ga or not extracted.version:
                return None

            target_tag = extracted.version.strip()
            namespace, repo = self.parse_repo_and_namespace(repo_str)
            download_url = f"https://hub.docker.com/v2/repositories/{namespace}/{repo}/tags/{target_tag}"

            return {
                "version": target_tag,
                "download_url": download_url,
                "filename": extracted.filename or f"{repo_str}:{target_tag}",
                "channel": extracted.release_channel or "production-stable",
                "release_notes": extracted.reasoning or f"Docker Hub container image tag '{target_tag}' for {repo_str}.",
                "tag_name": target_tag,
                "prerelease": False,
                "draft": False
            }
        except Exception as e:
            clean_msg = clean_error_message(e)
            print(f"[WARN] DockerHubProvider LLM extraction error for {package_id}: {clean_msg}")
            return None

    def get_latest_candidate(self, package_id: str, source_url: str, current_version: str) -> Optional[Dict[str, Any]]:
        namespace, repo = self.parse_repo_and_namespace(source_url)
        repo_str = f"{namespace}/{repo}"

        # Detect tag variant/flavor (e.g. "base-ubuntu24.04" in "13.0.0-base-ubuntu24.04")
        flavor = None
        m_flavor = re.match(r"^v?[0-9]+(?:\.[0-9]+)*(?:-(.+))?$", current_version)
        if m_flavor and m_flavor.group(1):
            flavor = m_flavor.group(1)

        # Query Docker Hub tags API (filtered by flavor first, then unfiltered)
        api_url = f"https://hub.docker.com/v2/repositories/{namespace}/{repo}/tags"

        def _tags(params: Dict[str, Any]) -> List[Dict[str, Any]]:
            try:
                resp = http_client.get(api_url, params=params)
                return resp.json().get("results", []) if resp.status_code == 200 else []
            except (requests.RequestException, ValueError) as e:
                print(f"[WARN] DockerHubProvider query error: {e}")
                return []

        results = _tags({"page_size": 50, "name": flavor} if flavor else {"page_size": 50})
        if not results and flavor:
            results = _tags({"page_size": 100})

        if not results:
            return None

        # Filter candidate tags matching the flavor (if any)
        candidate_tags = []
        for t in results:
            name = t.get("name", "")
            if flavor and not name.endswith(f"-{flavor}") and name != flavor:
                continue
            lower = name.lower()
            if any(x in lower for x in ["-rc", "-beta", "-alpha", "-preview", "-test", "-dev", "dirty"]):
                continue
            candidate_tags.append({
                "name": name,
                "last_updated": t.get("last_updated"),
                "digest": t.get("digest"),
                "architectures": [img.get("architecture") for img in t.get("images", []) if img.get("architecture")]
            })

        if not candidate_tags:
            return None

        # 1. Try LLM extraction if LLM client available
        if self.llm:
            cand = self._extract_with_llm(package_id, repo_str, current_version, candidate_tags)
            if cand:
                return cand

        # 2. Deterministic SemVer sort fallback
        parsed_cands = []
        for t in candidate_tags:
            name = t["name"]
            tm = re.match(r"^v?([0-9]+(?:\.[0-9]+)*)(?:-(.+))?$", name)
            if not tm:
                continue
            v_tuple = tuple(int(x) for x in tm.group(1).split("."))
            parsed_cands.append((v_tuple, name, t))

        if not parsed_cands:
            return None

        parsed_cands.sort(key=lambda x: x[0], reverse=True)
        best_tuple, best_name, best_meta = parsed_cands[0]

        tag_url = f"https://hub.docker.com/v2/repositories/{namespace}/{repo}/tags/{best_name}"
        return {
            "version": best_name,
            "download_url": tag_url,
            "filename": f"{repo_str}:{best_name}",
            "channel": "production-stable",
            "release_notes": f"Docker Hub container image tag '{best_name}' for {repo_str} (updated {best_meta.get('last_updated')}).",
            "tag_name": best_name,
            "prerelease": False,
            "draft": False
        }


# ==============================================================================
# Source Qualification Agent (Section 4.2)
# ==============================================================================


class SourceQualificationAgent:
    """Coordinates upstream query, LLM GA stability check, upfront rules, and candidate creation."""

    def __init__(self, store: Optional[DataStore] = None, model: Optional[str] = None, use_llm: bool = True):
        self.store = store or get_datastore()
        self.model = model or CONFIG.llm.model
        self.llm: Optional[LLMClient] = get_llm_client(self.model) if use_llm else None
        self.rule_checker = UpfrontRuleChecker(self.store)
        self.archive_provider = ArchiveScraperProvider(self.llm)
        self.github_provider = GitHubReleaseProvider(self.llm)
        self.manifest_provider = ManifestRegexProvider(self.llm)
        self.apt_provider = AptRepoProvider(self.llm)
        self.mft_provider = MftProvider(self.llm)
        self.docker_provider = DockerHubProvider(self.llm)

    def _update_package_db(self, package_id: str, upstream_version: str, summary: str, policy_status: Optional[str] = None):
        """Persists package state in the state store without overwriting policy status."""
        updates = {
            "upstream_version": upstream_version,
            "qualification_summary": summary
        }
        if policy_status:
            updates["status"] = policy_status
        self.store.update_package(package_id, updates)

    def summarize_release_notes(self, package_id: str, version: str, release_notes: str) -> str:
        """Uses Gemini LLM to summarize upstream release notes into 1-2 concise sentences."""
        if not release_notes or not str(release_notes).strip():
            return f"Qualified upstream GA release {version}."

        clean_notes = str(release_notes).strip()
        # If release notes are minimal or already a simple qualification string, keep as-is
        if len(clean_notes) < 40:
            return clean_notes

        if not self.llm:
            return clean_notes[:200]

        try:
            prompt = get_release_summary_prompt(
                package_id=package_id,
                version=version,
                release_notes=clean_notes[:4000]
            )
            text = self.llm.generate_text(prompt).replace("\n", " ")
            return text or f"Qualified upstream GA release {version}."
        except Exception as ex:
            print(f"[WARN] Failed to summarize release notes for {package_id}: {clean_error_message(ex)}")
            return clean_notes[:200]

    def qualify_package(self, package_id: str) -> Dict[str, Any]:
        pkg = self.store.get_package(package_id)
        if not pkg:
            return {"status": PackageStatus.ERROR, "message": f"Package {package_id} not found in database"}

        pkg_name = pkg.get("name", "")
        current_ver = pkg.get("current_version", "")
        source_url = pkg.get("source_url", "")
        upstream_type = pkg.get("upstream_type", "generic")
        pkg = policy.release_expired_snooze(self.store, pkg)  # Automatic snooze wake-up (Doc Section 3.1)
        current_policy_status = pkg.get("status", PackageStatus.REGISTERED)

        # If package is explicitly OBSOLETE, respect long-term deprecation policy
        if current_policy_status == PackageStatus.OBSOLETE:
            summary = "Package is currently marked OBSOLETE."
            return {
                "package_id": package_id,
                "name": pkg_name,
                "current_version": current_ver,
                "upstream_version": "-",
                "status": PackageStatus.OBSOLETE,
                "summary": summary
            }

        try:
            # 1. Discover upstream candidate dynamically from DataStore configuration
            candidate = None
            if upstream_type == "archive_scraper":
                candidate = self.archive_provider.get_latest_candidate(
                    package_id=package_id,
                    source_url=source_url
                )
            elif upstream_type == "raw_manifest":
                candidate = self.manifest_provider.get_candidate(
                    package_id=package_id,
                    manifest_url=source_url,
                    current_version=current_ver
                )
            elif upstream_type == "github_release":
                candidate = self.github_provider.get_latest_candidate(
                    package_id=package_id,
                    source_url=source_url,
                    current_version=current_ver
                )
            elif upstream_type == "apt_repository":
                candidate = self.apt_provider.get_latest_candidate(
                    package_id=package_id,
                    source_url=source_url
                )
            elif upstream_type in ("docker_hub", "container_image", "container_registry"):
                candidate = self.docker_provider.get_latest_candidate(
                    package_id=package_id,
                    source_url=source_url,
                    current_version=current_ver
                )
            elif upstream_type == "mft_api":
                candidate = self.mft_provider.get_latest_candidate(package_id, source_url=source_url)

            if not candidate:
                summary = f"Upstream source has no newer release than currently deployed version ({current_ver})."
                status_to_report = current_policy_status if current_policy_status in POLICY_STATUSES else PackageStatus.UP_TO_DATE
                self._update_package_db(package_id, "-", summary, policy_status=status_to_report)
                return {
                    "package_id": package_id,
                    "name": pkg_name,
                    "current_version": current_ver,
                    "upstream_version": "-",
                    "candidate_version": None,
                    "status": status_to_report,
                    "summary": summary
                }

            upstream_version = candidate["version"]
            # Preserve 'v' prefix if deployed blueprint version or tag uses 'v' prefix (e.g. v1.1.0 -> v1.1.1)
            if current_ver and current_ver.startswith("v") and not upstream_version.startswith("v"):
                upstream_version = f"v{upstream_version}"
            elif current_ver and not current_ver.startswith("v") and upstream_version.startswith("v"):
                upstream_version = upstream_version.lstrip("v")
            candidate["version"] = upstream_version
            download_url = candidate["download_url"]

            # Check if upstream candidate is strictly newer than current version
            curr_semver = parse_semver(current_ver)
            cand_semver = parse_semver(upstream_version)
            if curr_semver and cand_semver:
                if cand_semver <= curr_semver:
                    if cand_semver < curr_semver:
                        summary = f"Upstream version ({upstream_version}) is older than deployed blueprint ({current_ver})."
                    else:
                        summary = f"Deployed blueprint matches latest upstream release ({upstream_version})."
                    status_to_report = current_policy_status if current_policy_status in POLICY_STATUSES else PackageStatus.UP_TO_DATE
                    self._update_package_db(package_id, upstream_version, summary, policy_status=status_to_report)
                    return {
                        "package_id": package_id,
                        "name": pkg_name,
                        "current_version": current_ver,
                        "upstream_version": upstream_version,
                        "candidate_version": None,
                        "status": status_to_report,
                        "summary": summary
                    }
            elif upstream_version == current_ver:
                summary = f"Already at latest upstream version ({current_ver})."
                status_to_report = current_policy_status if current_policy_status in POLICY_STATUSES else PackageStatus.UP_TO_DATE
                self._update_package_db(package_id, upstream_version, summary, policy_status=status_to_report)
                return {
                    "package_id": package_id,
                    "name": pkg_name,
                    "current_version": current_ver,
                    "upstream_version": upstream_version,
                    "candidate_version": None,
                    "status": status_to_report,
                    "summary": summary
                }

            # 2. GA Stability Gate (Evaluated directly by upstream provider's LLM extraction)
            if candidate.get("prerelease") or candidate.get("channel") not in ("production-stable", "ga"):
                summary = f"Upstream release {upstream_version} discarded as non-GA: {candidate.get('release_notes', '')}"
                self._update_package_db(package_id, upstream_version, summary, policy_status=PackageStatus.UP_TO_DATE)
                return {
                    "package_id": package_id,
                    "name": pkg_name,
                    "current_version": current_ver,
                    "upstream_version": upstream_version,
                    "candidate_version": None,
                    "status": PackageStatus.UP_TO_DATE,
                    "summary": summary
                }

            # 3. Version-aware snooze & block policy gates (a strictly newer version is not held)
            hold = policy.policy_hold(pkg, upstream_version)
            if hold:
                hold_status, summary = hold
                self._update_package_db(package_id, upstream_version, summary, policy_status=hold_status)
                return {
                    "package_id": package_id,
                    "name": pkg_name,
                    "current_version": current_ver,
                    "upstream_version": upstream_version,
                    "candidate_version": None,
                    "status": hold_status,
                    "summary": summary
                }

            # 4. Upfront Learned Rule Check (Section 4.2 & Case 3)
            is_blocked, rule = self.rule_checker.check_version(package_id, upstream_version)
            if is_blocked:
                summary = f"Version {upstream_version} blocked by rule '{rule['rule_id']}': {rule['reason']}"
                # Record the blocked version so a newer upstream release is evaluated again.
                self.store.update_package(package_id, {"blocked_version": upstream_version})
                self._update_package_db(package_id, upstream_version, summary, policy_status=PackageStatus.BLOCKED)
                return {
                    "package_id": package_id,
                    "name": pkg_name,
                    "current_version": current_ver,
                    "upstream_version": upstream_version,
                    "candidate_version": None,
                    "status": PackageStatus.BLOCKED,
                    "rule_id": rule["rule_id"],
                    "reason": rule["reason"],
                    "summary": summary
                }

            # An open PR already tracks this version: keep it (and its test state) instead of re-creating it.
            tracked = next((c for c in self.store.list_candidates(package_id=package_id)
                            if c.get("status") in PR_TRACKED_CANDIDATE_STATUSES and c.get("version") == upstream_version), None)
            if tracked:
                self.store.update_package(package_id, {"upstream_version": upstream_version})
                return {
                    "candidate_id": tracked["candidate_id"],
                    "package_id": package_id,
                    "name": pkg_name,
                    "current_version": current_ver,
                    "upstream_version": upstream_version,
                    "candidate_version": upstream_version,
                    "status": tracked["status"],
                    "summary": f"Update {upstream_version} already has an open PR ({tracked.get('pr_url')})."
                }

            # 4. Artifact Liveness Verification (Fast deterministic gate before LLM triage)
            if not http_client.is_url_live(download_url):
                summary = f"Release {upstream_version} download URL unreachable."
                self._update_package_db(package_id, upstream_version, summary, policy_status=PackageStatus.UNREACHABLE)
                return {
                    "package_id": package_id,
                    "name": pkg_name,
                    "current_version": current_ver,
                    "upstream_version": upstream_version,
                    "candidate_version": None,
                    "status": PackageStatus.UNREACHABLE,
                    "summary": summary
                }

            # 5. Record Candidate Update directly upon passing GA checks, policy rules, and artifact liveness
            candidate_id = f"cand-{str(uuid.uuid4())[:8]}"
            rel_notes = candidate.get("release_notes") or candidate.get("reasoning") or ""
            cand_summary = self.summarize_release_notes(package_id, upstream_version, rel_notes)
            # Replace stale UPDATE_FOUND candidates; never drop merged, held, or PR-tracked ones.
            self.store.delete_candidates(package_id, exclude_status=(
                CandidateStatus.MERGED, *POLICY_STATUSES, *PR_TRACKED_CANDIDATE_STATUSES))
            self.store.save_candidate({
                "candidate_id": candidate_id,
                "package_id": package_id,
                "version": upstream_version,
                "current_version": current_ver,
                "download_url": download_url,
                "checksum": candidate.get("checksum_sha256") or candidate.get("checksum"),
                "pr_url": None,
                "build_url": None,
                "status": CandidateStatus.UPDATE_FOUND,
                "summary": cand_summary,
                "created_at": datetime.datetime.now(datetime.timezone.utc).isoformat()
            })

            summary = f"Update found ({upstream_version}): {cand_summary}"
            self._update_package_db(package_id, upstream_version, summary, policy_status=PackageStatus.UPDATE_FOUND)

            return {
                "candidate_id": candidate_id,
                "package_id": package_id,
                "name": pkg_name,
                "current_version": current_ver,
                "upstream_version": upstream_version,
                "candidate_version": upstream_version,
                "download_url": download_url,
                "filename": candidate.get("filename", ""),
                "status": PackageStatus.UPDATE_FOUND,
                "summary": summary,
                "release_channel": candidate.get("channel", "production-stable")
            }
        except Exception as ex:
            clean_err = clean_error_message(ex)
            summary = f"Qualification error: {clean_err}"
            self._update_package_db(package_id, "-", summary, policy_status=PackageStatus.ERROR)
            return {
                "package_id": package_id,
                "name": pkg_name,
                "current_version": current_ver,
                "upstream_version": "-",
                "candidate_version": None,
                "status": PackageStatus.ERROR,
                "summary": summary,
                "error": clean_err
            }

    def qualify_all(self) -> List[Dict[str, Any]]:
        packages = self.store.list_packages()
        package_ids = [p["package_id"] for p in packages]

        results = []
        total = len(package_ids)
        for i, pkg_id in enumerate(package_ids, 1):
            print(f"[{i}/{total}] Evaluating package '{pkg_id}'...", flush=True)
            res = self.qualify_package(pkg_id)
            results.append(res)
            cand_disp = res.get('candidate_version') or '-'
            up_disp = res.get('upstream_version') or '-'
            wf_status = res.get('workflow_status') or res.get('status')
            print(f"       -> Status: {wf_status} | Upstream: {up_disp} | Target: {cand_disp}", flush=True)
        return results

if __name__ == "__main__":
    agent = SourceQualificationAgent()
    print("[RUNNING] Running Source Qualification Agent...")
    all_res = agent.qualify_all()
    for r in all_res:
        wf = r.get("workflow_status") or r.get("status")
        if wf == PackageStatus.UPDATE_FOUND:
            print(f"[UPDATE_FOUND] {r['package_id']} -> {r['candidate_version']}")
            print(f"  - Summary: {r.get('summary')}")
        else:
            print(f"[{wf}] {r['package_id']}: {r.get('summary')}")
