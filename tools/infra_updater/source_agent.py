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
  5. Performs Gemini LLM semantic changelog & deprecation triage.
"""

import datetime
import gzip
import json
import os
import re
import subprocess
import sys
import time
import uuid
import urllib.parse
import urllib.request
from typing import Dict, List, Optional, Tuple, Any
import requests
from packaging.version import Version
from packaging.specifiers import SpecifierSet
from pydantic import BaseModel, Field

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
if BASE_DIR not in sys.path:
    sys.path.insert(0, BASE_DIR)
REPO_ROOT = os.path.dirname(os.path.dirname(BASE_DIR))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

from tools.infra_updater.datastore import DataStore, get_datastore
from tools.infra_updater.prompts import (
    load_prompt,
    get_archive_scraper_prompt,
    get_manifest_regex_prompt,
    get_github_release_prompt,
    get_apt_repo_prompt,
    get_compute_image_prompt,
    get_changelog_triage_prompt,
)

# Enable Google GenAI SDK (importing from hackathon conda environment if not in current sys.path)
hackathon_site = "/usr/local/google/home/rahimkh/miniconda3/envs/hackathon/lib/python3.10/site-packages"
if os.path.exists(hackathon_site) and hackathon_site not in sys.path:
    sys.path.append(hackathon_site)

try:
    from google import genai
    from google.genai import types
    GENAI_AVAILABLE = True
except ImportError:
    GENAI_AVAILABLE = False

# ==============================================================================
# Pydantic Structured Contracts for LLM Function Calling / Structured Output
# ==============================================================================

class ChangelogSemanticAnalysis(BaseModel):
    is_breaking: bool = Field(description="True if this release contains breaking changes, dropped OS/kernel support, or renamed arguments")
    compatibility_verdict: str = Field(description="Verdict: 'COMPATIBLE', 'POTENTIALLY_BREAKING', or 'INCOMPATIBLE'")
    breaking_reasons: List[str] = Field(description="List of specific breaking changes or deprecation notices detected in changelog")
    summary: str = Field(description="Concise 1-2 sentence executive summary of bug fixes, features, and driver changes")
    pr_changelog_snippet: str = Field(description="Markdown formatted bullet points suitable for direct insertion into PR description")

class CandidateReleaseExtraction(BaseModel):
    version: str = Field(description="Normalized semantic version of the release, e.g., '13.4.2' or '1.4.11'")
    download_url: str = Field(description="Direct HTTP/HTTPS download URL for the target installer artifact or repository")
    filename: str = Field(description="Filename of the installer artifact, e.g. 'cuda_13.4.2_linux_sbsa.run' or 'gve-1.4.11.deb'")
    is_production_ga: bool = Field(description="True if this is a production-stable General Availability (GA) release, False if preview, RC, or beta")
    release_channel: str = Field(description="Release channel name, e.g. 'production-stable', 'developer-preview', 'release-candidate'")
    target_architecture: Optional[str] = Field(default=None, description="Architecture name if applicable, e.g. 'arm64-sbsa' or 'x86_64'")
    reasoning: str = Field(description="Brief explanation of why this version and artifact were chosen as the latest stable GA release")


def clean_error_message(ex: Any) -> str:
    """Extracts a concise, user-friendly error message without dumping raw JSON or stacktraces."""
    raw = str(ex).strip()
    raw_lower = raw.lower()

    if "429" in raw or "resource_exhausted" in raw_lower or "quota" in raw_lower or "rate_limit" in raw_lower:
        return "Gemini API rate limit or quota exceeded (429 RESOURCE_EXHAUSTED). Please retry shortly."
    if "decode_preempted" in raw_lower or "preempted out of decode queue" in raw_lower:
        return "Vertex AI inference preempted by cluster capacity (DECODE_PREEMPTED). Please retry."
    if "503" in raw or "unavailable" in raw_lower:
        return "Gemini service temporarily unavailable (503 UNAVAILABLE). Please retry shortly."
    if "401" in raw or "403" in raw or "permission_denied" in raw_lower:
        return "Authentication or permission error when contacting Gemini API (401/403)."
    if "deadline" in raw_lower or "504" in raw:
        return "Gemini request timed out (DEADLINE_EXCEEDED). Please retry."

    # Look for a clean message inside JSON-like output
    m = re.search(r'"message":\s*"([^"]+)"', raw)
    if m:
        msg = m.group(1).split("\n")[0].strip()
        if len(msg) > 95:
            msg = msg[:92] + "..."
        return msg

    # Strip any proto type URLs and JSON braces
    cleaned = raw.split("\n")[0].split("[type.googleapis.com")[0].split("{")[0].strip().rstrip(".:")
    if len(cleaned) > 95:
        cleaned = cleaned[:92] + "..."
    return cleaned or "LLM generation encountered an unexpected error."


def generate_content_with_retry(
    client: Any,
    model: str,
    contents: Any,
    config: Any,
    max_retries: int = 3,
    initial_delay: float = 2.0
) -> Any:
    """Invokes client.models.generate_content with exponential backoff on 429, 503, or preemption."""
    delay = initial_delay
    last_ex = None
    for attempt in range(max_retries):
        try:
            return client.models.generate_content(
                model=model,
                contents=contents,
                config=config
            )
        except Exception as ex:
            last_ex = ex
            err_str = str(ex).lower()
            is_retriable = (
                "429" in err_str
                or "resource_exhausted" in err_str
                or "rate_limit" in err_str
                or "quota" in err_str
                or "503" in err_str
                or "unavailable" in err_str
                or "preempted" in err_str
                or "deadline" in err_str
            )
            if is_retriable and attempt < max_retries - 1:
                print(f"[WARN] Gemini LLM transient error (attempt {attempt + 1}/{max_retries}), retrying in {delay:.1f}s: {clean_error_message(ex)}")
                time.sleep(delay)
                delay *= 2
            else:
                break
    raise last_ex


def check_http_liveness(url: str) -> bool:
    """Confirms artifact URL returns HTTP 200."""
    headers = {"User-Agent": "ClusterToolkitInfraUpdater/1.0"}
    try:
        resp = requests.head(url, timeout=8, headers=headers, allow_redirects=True)
        if resp.status_code == 200:
            return True
        if resp.status_code in (403, 405, 429):
            resp_get = requests.get(url, timeout=8, headers=headers, stream=True, allow_redirects=True)
            return resp_get.status_code == 200
        return False
    except Exception:
        return False

def parse_semver(v_str: str) -> Optional[Version]:
    """Extracts a clean semver Version object from diverse version formats."""
    try:
        clean = re.sub(r'^[vV]', '', v_str.strip())
        clean = re.sub(r'^[0-9]+:', '', clean)
        clean = clean.split('_')[0]
        clean = clean.split('-')[0]
        return Version(clean)
    except Exception:
        return None

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

    def __init__(self, llm_client=None, model: str = "gemini-3.8-flash"):
        self.llm_client = llm_client
        self.model = model

    def _extract_with_llm(self, package_id: str, page_url: str, html_content: str, target_arch: str) -> Optional[Dict[str, Any]]:
        if not self.llm_client or not GENAI_AVAILABLE:
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
            resp = generate_content_with_retry(
                client=self.llm_client,
                model=self.model,
                contents=prompt,
                config=types.GenerateContentConfig(
                    response_mime_type="application/json",
                    response_schema=CandidateReleaseExtraction,
                    temperature=0.0
                )
            )
            cand = json.loads(resp.text)
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

        if not self.llm_client or not GENAI_AVAILABLE:
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
                r = requests.get(u, timeout=8)
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

    def __init__(self, llm_client=None, model: str = "gemini-3.8-flash"):
        self.llm_client = llm_client
        self.model = model

    def _extract_with_llm(self, package_id: str, manifest_url: str, content: str) -> Optional[Dict[str, Any]]:
        if not self.llm_client or not GENAI_AVAILABLE:
            return None

        prompt = get_manifest_regex_prompt(
            package_id=package_id,
            manifest_url=manifest_url,
            manifest_content=content[:2500]
        )
        try:
            resp = generate_content_with_retry(
                client=self.llm_client,
                model=self.model,
                contents=prompt,
                config=types.GenerateContentConfig(
                    response_mime_type="application/json",
                    response_schema=CandidateReleaseExtraction,
                    temperature=0.0
                )
            )
            data = json.loads(resp.text)
            extracted = CandidateReleaseExtraction(**data)
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

    def get_candidate(self, package_id: str, manifest_url: str) -> Optional[Dict[str, Any]]:
        if not self.llm_client or not GENAI_AVAILABLE:
            raise RuntimeError(f"Gemini LLM client is required for ManifestProvider on {package_id}")

        resp = requests.get(manifest_url, timeout=8)
        if resp.status_code != 200:
            raise RuntimeError(f"Failed to fetch manifest from {manifest_url} (HTTP {resp.status_code})")
        content = resp.text

        llm_cand = self._extract_with_llm(package_id, manifest_url, content)
        if not llm_cand:
            raise RuntimeError(f"LLM could not discover or qualify upstream GA release for {package_id} from manifest {manifest_url}")
        return llm_cand


class GitHubReleaseProvider:
    """Discovers latest GA release via Gemini LLM extraction over upstream releases/tags."""

    def __init__(self, llm_client=None, model: str = "gemini-3.8-flash"):
        self.llm_client = llm_client
        self.model = model

    @staticmethod
    def extract_repo_from_url(url: str) -> Optional[str]:
        m = re.search(r"github\.com/([^/]+/[^/\s]+)", url)
        return m.group(1).rstrip("/") if m else None

    def _extract_with_llm(self, package_id: str, source_url: str, releases: List[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
        if not self.llm_client or not GENAI_AVAILABLE:
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
            candidates_summary_json=json.dumps(candidates_summary, indent=2)
        )
        try:
            resp = generate_content_with_retry(
                client=self.llm_client,
                model=self.model,
                contents=prompt,
                config=types.GenerateContentConfig(
                    response_mime_type="application/json",
                    response_schema=CandidateReleaseExtraction,
                    temperature=0.0
                )
            )
            data = json.loads(resp.text)
            extracted = CandidateReleaseExtraction(**data)
            if not extracted.is_production_ga or not extracted.version:
                return None

            clean_ver = extracted.version.lstrip("v")
            matching_rel = next((r for r in releases if extracted.version in r.get("tag_name", "") or clean_ver in r.get("tag_name", "")), None)
            release_notes = matching_rel.get("body", "") if matching_rel else (extracted.reasoning or f"Upstream GA release {clean_ver}.")

            return {
                "version": clean_ver,
                "download_url": extracted.download_url,
                "filename": extracted.filename or f"{package_id}-{clean_ver}.tar.gz",
                "channel": extracted.release_channel or "production-stable",
                "release_notes": release_notes,
                "tag_name": extracted.version,
                "prerelease": False,
                "draft": False
            }
        except Exception as e:
            clean_msg = clean_error_message(e)
            print(f"[WARN] GitHubReleaseProvider LLM extraction error for {package_id}: {clean_msg}")
            raise RuntimeError(f"LLM extraction failed for {package_id} from GitHub {source_url}: {clean_msg}")

    def get_latest_candidate(self, package_id: str, source_url: str) -> Optional[Dict[str, Any]]:
        repo = self.extract_repo_from_url(source_url)
        if not repo:
            raise ValueError(f"Could not extract GitHub repository from {source_url}")

        url = f"https://api.github.com/repos/{repo}/releases?per_page=15"
        headers = {"User-Agent": "ClusterToolkitInfraUpdater/1.0"}
        resp = requests.get(url, timeout=8, headers=headers)
        releases = resp.json() if resp.status_code == 200 else []

        # If no releases published, fall back to git tags (e.g. openmpi)
        if not releases or not isinstance(releases, list):
            tags_url = f"https://api.github.com/repos/{repo}/tags?per_page=15"
            tags_resp = requests.get(tags_url, timeout=8, headers=headers)
            if tags_resp.status_code == 200 and isinstance(tags_resp.json(), list):
                releases = [
                    {
                        "tag_name": t.get("name", ""),
                        "html_url": f"https://github.com/{repo}/releases/tag/{t.get('name', '')}",
                        "body": f"Release tag {t.get('name', '')} from {repo}.",
                        "prerelease": any(x in t.get("name", "").lower() for x in ["rc", "beta", "alpha", "preview"]),
                        "draft": False,
                        "assets": []
                    }
                    for t in tags_resp.json()
                ]

        if not releases or not isinstance(releases, list):
            raise RuntimeError(f"No releases or tags returned by GitHub API for {repo}")

        llm_cand = self._extract_with_llm(package_id, source_url, releases)
        if not llm_cand:
            raise RuntimeError(f"LLM could not discover or qualify upstream GA release for {package_id} from {source_url}")
        return llm_cand


class ComputeImageProvider:
    """Discovers upstream compute engine image information via active GCP image family with Gemini LLM qualification."""

    def __init__(self, llm_client=None, model: str = "gemini-3.8-flash"):
        self.llm_client = llm_client
        self.model = model

    @staticmethod
    def parse_family_and_project(package_id: str, source_url: str, current_version: str) -> Tuple[str, str]:
        """Dynamically extracts GCP project and image family from source URL, version, or package metadata."""
        clean_url = (source_url or "").strip()
        # 1. Match full GCP resource URI: projects/<project>/global/images/family/<family>
        m_full = re.search(r"projects/([^/]+)/(?:global/)?images/family/([^/\s]+)", clean_url)
        if m_full:
            return m_full.group(2), m_full.group(1)

        # 2. Match standard <project>/<family> format in source_url
        if "/" in clean_url and not clean_url.startswith("http"):
            parts = clean_url.split("/", 1)
            return parts[1].strip(), parts[0].strip()

        # 3. Match <project>/<family> in current_version
        clean_ver = (current_version or "").strip()
        if "/" in clean_ver and not clean_ver.startswith("http"):
            parts = clean_ver.split("/", 1)
            return parts[1].strip(), parts[0].strip()

        # 4. Extract project from URL if present, otherwise infer from package domain
        m_proj = re.search(r"projects/([^/\s]+)", clean_url)
        if m_proj:
            project = m_proj.group(1)
        elif "slurm" in package_id.lower():
            project = "schedmd-slurm-public"
        elif "accelerator" in package_id.lower() or "ubuntu" in package_id.lower():
            project = "ubuntu-os-accelerator-images"
        else:
            project = "schedmd-slurm-public"

        family = clean_ver if clean_ver else package_id
        return family, project

    def get_candidate(self, package_id: str, current_version: str, source_url: str = "") -> Optional[Dict[str, Any]]:
        family, project = self.parse_family_and_project(package_id, source_url, current_version)

        image_data = None
        try:
            cmd = [
                "gcloud", "compute", "images", "describe-from-family",
                family, f"--project={project}", "--format=json"
            ]
            res = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, check=False)
            if res.returncode == 0 and res.stdout.strip():
                image_data = json.loads(res.stdout)
        except Exception as ex:
            print(f"[WARN] gcloud execution failed for family '{family}' in '{project}': {clean_error_message(ex)}")

        if not image_data:
            image_data = {
                "family": family,
                "project": project,
                "name": f"{family}-active-build",
                "status": "READY",
                "creationTimestamp": datetime.datetime.now(datetime.timezone.utc).isoformat()
            }

        img_name = image_data.get("name", family)
        creation = image_data.get("creationTimestamp", "")[:10]
        self_link = image_data.get("selfLink", f"https://console.cloud.google.com/compute/imagesDetail/projects/{project}/global/images/{img_name}")

        # Generalized LLM reasoning for image release qualification
        if self.llm_client and GENAI_AVAILABLE:
            try:
                metadata_summary = {
                    "name": img_name,
                    "family": family,
                    "project": project,
                    "status": image_data.get("status", "READY"),
                    "creationTimestamp": creation,
                    "description": image_data.get("description", ""),
                    "deprecated": image_data.get("deprecated"),
                    "guestOsFeatures": [f.get("type") for f in image_data.get("guestOsFeatures", [])]
                }
                prompt = get_compute_image_prompt(
                    family=family,
                    project=project,
                    package_id=package_id,
                    image_metadata_json=json.dumps(metadata_summary, indent=2)
                )
                resp = generate_content_with_retry(
                    client=self.llm_client,
                    model=self.model,
                    contents=prompt,
                    config=types.GenerateContentConfig(
                        response_mime_type="application/json",
                        response_schema=CandidateReleaseExtraction,
                        temperature=0.0
                    )
                )
                cand = json.loads(resp.text)
                extracted = CandidateReleaseExtraction(**cand)

                return {
                    "version": extracted.version or family,
                    "image_name": extracted.filename or img_name,
                    "download_url": extracted.download_url or self_link,
                    "filename": extracted.filename or img_name,
                    "channel": extracted.release_channel or "production-stable",
                    "release_notes": extracted.reasoning or f"Google Cloud Compute Engine Image Family '{family}' in project '{project}' (Build: {img_name}, Status: READY).",
                    "tag_name": img_name,
                    "prerelease": not extracted.is_production_ga,
                    "draft": False,
                    "build_date": creation
                }
            except Exception as lex:
                clean_msg = clean_error_message(lex)
                print(f"[WARN] ComputeImageProvider LLM qualification error for {package_id}: {clean_msg}")

        return {
            "version": family,
            "image_name": img_name,
            "download_url": self_link,
            "filename": img_name,
            "channel": "production-stable",
            "release_notes": f"Compute Engine Image Family '{family}' in project '{project}' (Build: {img_name}).",
            "tag_name": img_name,
            "prerelease": False,
            "draft": False,
            "build_date": creation
        }


class AptRepoProvider:
    """Discovers package versions dynamically from official APT package repository index (Packages.gz)."""

    def __init__(self, llm_client=None, model: str = "gemini-3.8-flash"):
        self.llm_client = llm_client
        self.model = model
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

        if not self.llm_client or not GENAI_AVAILABLE:
            raise RuntimeError(f"Gemini LLM client is required for AptRepoProvider on {package_id}")

        index_url, repo_base = self.resolve_index_url(source_url, distro=distro, arch=arch)
        req = urllib.request.Request(index_url, headers={"User-Agent": "ClusterToolkitInfraUpdater/1.0"})
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = gzip.decompress(resp.read()).decode("utf-8", errors="ignore")

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
            resp = generate_content_with_retry(
                client=self.llm_client,
                model=self.model,
                contents=prompt,
                config=types.GenerateContentConfig(
                    response_mime_type="application/json",
                    response_schema=CandidateReleaseExtraction,
                    temperature=0.0
                )
            )
            data_json = json.loads(resp.text)
            extracted = CandidateReleaseExtraction(**data_json)
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
            req = urllib.request.Request(
                self.API_URL,
                data=urllib.parse.urlencode({"action": "get_versions"}).encode("utf-8"),
                headers={"User-Agent": "ClusterToolkitInfraUpdater/1.0"}
            )
            with urllib.request.urlopen(req, timeout=10) as resp:
                data = json.loads(resp.read().decode("utf-8"))

            latest_version = data.get("latest")
            if not latest_version and data.get("ga"):
                latest_version = data["ga"][0]
            if not latest_version:
                return None

            req2 = urllib.request.Request(
                self.API_URL,
                data=urllib.parse.urlencode({
                    "action": "get_download_info",
                    "version": latest_version,
                    "distro": "Linux",
                    "os": "DEB based",
                    "arch": arch
                }).encode("utf-8"),
                headers={"User-Agent": "ClusterToolkitInfraUpdater/1.0"}
            )
            with urllib.request.urlopen(req2, timeout=10) as resp2:
                info = json.loads(resp2.read().decode("utf-8"))

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


# ==============================================================================
# Source Qualification Agent (Section 4.2)
# ==============================================================================


class SourceQualificationAgent:
    """Coordinates upstream query, LLM GA stability check, upfront rules, and candidate creation."""

    def __init__(self, store: Optional[DataStore] = None, model: str = "gemini-3.8-flash", location: str = "global", use_llm: bool = True):
        self.store = store or get_datastore()
        self.model = model
        self.location = os.environ.get("GOOGLE_CLOUD_REGION", location)
        self.use_llm = use_llm and GENAI_AVAILABLE
        self.rule_checker = UpfrontRuleChecker(self.store)
        self.archive_provider = ArchiveScraperProvider()
        self.github_provider = GitHubReleaseProvider()
        self.manifest_provider = ManifestRegexProvider()
        self.image_provider = ComputeImageProvider()
        self.apt_provider = AptRepoProvider()
        self.mft_provider = MftProvider()

        self.client = None
        if self.use_llm:
            try:
                api_key = os.environ.get("GEMINI_API_KEY")
                if api_key:
                    self.client = genai.Client(api_key=api_key)
                else:
                    project = os.environ.get("GOOGLE_CLOUD_PROJECT", "hpc-toolkit-dev")
                    self.client = genai.Client(vertexai=True, project=project, location=self.location)
                self.archive_provider.llm_client = self.client
                self.archive_provider.model = self.model
                self.github_provider.llm_client = self.client
                self.github_provider.model = self.model
                self.manifest_provider.llm_client = self.client
                self.manifest_provider.model = self.model
                self.apt_provider.llm_client = self.client
                self.apt_provider.model = self.model
                self.image_provider.llm_client = self.client
                self.image_provider.model = self.model
                self.mft_provider.llm_client = self.client
                self.mft_provider.model = self.model
            except Exception as ex:
                print(f"[WARN] Failed to initialize Gemini LLM Client: {ex}.")
                self.client = None
                self.use_llm = False

    def analyze_changelog_with_llm(self, package_id: str, version: str, release_notes: str) -> ChangelogSemanticAnalysis:
        """Invokes Gemini LLM to read upstream release notes and evaluate OS/kernel compatibility."""
        if not self.use_llm or not self.client:
            raise RuntimeError(f"Gemini LLM client is required for changelog analysis on {package_id}")

        prompt = get_changelog_triage_prompt(
            package_id=package_id,
            version=version,
            release_notes=release_notes
        )
        try:
            resp = generate_content_with_retry(
                client=self.client,
                model=self.model,
                contents=prompt,
                config=types.GenerateContentConfig(
                    response_mime_type="application/json",
                    response_schema=ChangelogSemanticAnalysis,
                    temperature=0.0
                )
            )
            data = json.loads(resp.text)
            return ChangelogSemanticAnalysis(**data)
        except Exception as e:
            clean_msg = clean_error_message(e)
            raise RuntimeError(f"LLM changelog analysis failed for {package_id} {version}: {clean_msg}")

    def _update_package_db(self, package_id: str, upstream_version: str, summary: str, policy_status: Optional[str] = None):
        """Persists package state in the state store without overwriting policy status."""
        updates = {
            "upstream_version": upstream_version,
            "qualification_summary": summary
        }
        if policy_status:
            updates["status"] = policy_status
        self.store.update_package(package_id, updates)

    def check_http_liveness(self, url: str) -> bool:
        """Confirms artifact URL returns HTTP 200 via canonical helper."""
        return check_http_liveness(url)

    def qualify_package(self, package_id: str) -> Dict[str, Any]:
        pkg = self.store.get_package(package_id)
        if not pkg:
            return {"status": "ERROR", "message": f"Package {package_id} not found in database"}

        pkg_name = pkg.get("name", "")
        current_ver = pkg.get("current_version", "")
        source_url = pkg.get("source_url", "")
        upstream_type = pkg.get("upstream_type", "generic")
        current_policy_status = pkg.get("status", "REGISTERED")
        snooze_until = pkg.get("snooze_until")

        # Automatic snooze wake-up check (Doc Section 3.1)
        if current_policy_status == "SNOOZED" and snooze_until:
            try:
                now_str = datetime.datetime.now(datetime.timezone.utc).isoformat()
                if str(snooze_until) <= now_str:
                    self.store.update_package(package_id, {"status": "REGISTERED", "snooze_until": None})
                    current_policy_status = "REGISTERED"
            except Exception:
                pass

        # If package is explicitly snoozed, blocked, or obsolete, respect long-term policy (Section 3.1)
        if current_policy_status in ("SNOOZED", "OBSOLETE", "BLOCKED"):
            summary = f"Package is currently {current_policy_status}."
            return {
                "package_id": package_id,
                "name": pkg_name,
                "current_version": current_ver,
                "upstream_version": "-",
                "status": current_policy_status,
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
                    manifest_url=source_url
                )
            elif upstream_type == "github_release":
                candidate = self.github_provider.get_latest_candidate(
                    package_id=package_id,
                    source_url=source_url
                )
            elif upstream_type == "gcp_compute_image":
                candidate = self.image_provider.get_candidate(
                    package_id=package_id,
                    current_version=current_ver,
                    source_url=source_url
                )
                if candidate:
                    img_name = candidate.get("image_name", current_ver)
                    build_date = candidate.get("build_date", "active")
                    summary = f"Active GCP compute image family '{current_ver}' (latest build: {img_name}, Status: READY, Build Date: {build_date})."
                    self._update_package_db(package_id, img_name, summary, policy_status="UP_TO_DATE")
                    return {
                        "package_id": package_id,
                        "name": pkg_name,
                        "current_version": current_ver,
                        "upstream_version": img_name,
                        "candidate_version": None,
                        "status": "UP_TO_DATE",
                        "summary": summary
                    }
            elif upstream_type == "apt_repository":
                candidate = self.apt_provider.get_latest_candidate(
                    package_id=package_id,
                    source_url=source_url
                )
            elif upstream_type in ("mft_api", "archive_scraper") or package_id == "mft":
                if package_id == "mft" or upstream_type == "mft_api":
                    candidate = self.mft_provider.get_latest_candidate(package_id, source_url=source_url)
                else:
                    candidate = self.archive_provider.get_latest_candidate(
                        package_id=package_id,
                        source_url=source_url
                    )

            if not candidate:
                summary = f"Upstream source has no newer release than currently deployed version ({current_ver})."
                self._update_package_db(package_id, "-", summary, policy_status="UP_TO_DATE")
                return {
                    "package_id": package_id,
                    "name": pkg_name,
                    "current_version": current_ver,
                    "upstream_version": "-",
                    "candidate_version": None,
                    "status": "UP_TO_DATE",
                    "summary": summary
                }

            upstream_version = candidate["version"]
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
                    self._update_package_db(package_id, upstream_version, summary, policy_status="UP_TO_DATE")
                    return {
                        "package_id": package_id,
                        "name": pkg_name,
                        "current_version": current_ver,
                        "upstream_version": upstream_version,
                        "candidate_version": None,
                        "status": "UP_TO_DATE",
                        "summary": summary
                    }
            elif upstream_version == current_ver:
                summary = f"Already at latest upstream version ({current_ver})."
                self._update_package_db(package_id, upstream_version, summary, policy_status="UP_TO_DATE")
                return {
                    "package_id": package_id,
                    "name": pkg_name,
                    "current_version": current_ver,
                    "upstream_version": upstream_version,
                    "candidate_version": None,
                    "status": "UP_TO_DATE",
                    "summary": summary
                }

            # 2. GA Stability Gate (Evaluated directly by upstream provider's LLM extraction)
            if candidate.get("prerelease") or candidate.get("channel") not in ("production-stable", "ga"):
                summary = f"Upstream release {upstream_version} discarded as non-GA: {candidate.get('release_notes', '')}"
                self._update_package_db(package_id, upstream_version, summary, policy_status="UP_TO_DATE")
                return {
                    "package_id": package_id,
                    "name": pkg_name,
                    "current_version": current_ver,
                    "upstream_version": upstream_version,
                    "candidate_version": None,
                    "status": "UP_TO_DATE",
                    "summary": summary
                }

            # 3. Upfront Learned Rule Check (Section 4.2 & Case 3)
            is_blocked, rule = self.rule_checker.check_version(package_id, upstream_version)
            if is_blocked:
                summary = f"Version {upstream_version} blocked by rule '{rule['rule_id']}': {rule['reason']}"
                self._update_package_db(package_id, upstream_version, summary, policy_status="BLOCKED")
                return {
                    "package_id": package_id,
                    "name": pkg_name,
                    "current_version": current_ver,
                    "upstream_version": upstream_version,
                    "candidate_version": None,
                    "status": "BLOCKED",
                    "rule_id": rule["rule_id"],
                    "reason": rule["reason"],
                    "summary": summary
                }

            # 4. Artifact Liveness Verification (Fast deterministic gate before LLM triage)
            if not self.check_http_liveness(download_url):
                summary = f"Release {upstream_version} download URL unreachable."
                self._update_package_db(package_id, upstream_version, summary, policy_status="UNREACHABLE")
                return {
                    "package_id": package_id,
                    "name": pkg_name,
                    "current_version": current_ver,
                    "upstream_version": upstream_version,
                    "candidate_version": None,
                    "status": "UNREACHABLE",
                    "summary": summary
                }

            # 5. LLM Semantic Changelog & Deprecation Analysis
            llm_changelog = self.analyze_changelog_with_llm(
                package_id, upstream_version, candidate.get("release_notes", "")
            )

            # 6. Record Candidate Update (Section 2.3 & 3.1: status = 'UPDATE_FOUND')
            candidate_id = f"cand-{str(uuid.uuid4())[:8]}"
            self.store.delete_candidates(package_id, exclude_status="MERGED")
            self.store.save_candidate({
                "candidate_id": candidate_id,
                "package_id": package_id,
                "version": upstream_version,
                "download_url": download_url,
                "checksum": candidate.get("checksum_sha256") or candidate.get("checksum"),
                "pr_url": None,
                "build_url": None,
                "status": "UPDATE_FOUND",
                "compatibility_verdict": llm_changelog.compatibility_verdict,
                "changelog_summary": llm_changelog.summary,
                "created_at": datetime.datetime.now(datetime.timezone.utc).isoformat()
            })

            summary = f"Update found ({upstream_version}): {llm_changelog.summary}"
            self._update_package_db(package_id, upstream_version, summary, policy_status="UPDATE_FOUND")

            return {
                "candidate_id": candidate_id,
                "package_id": package_id,
                "name": pkg_name,
                "current_version": current_ver,
                "upstream_version": upstream_version,
                "candidate_version": upstream_version,
                "download_url": download_url,
                "filename": candidate.get("filename", ""),
                "status": "UPDATE_FOUND",
                "summary": summary,
                "llm_release_track": candidate.get("channel", "production-stable"),
                "llm_verdict": llm_changelog.compatibility_verdict,
                "llm_is_breaking": llm_changelog.is_breaking,
                "llm_breaking_reasons": llm_changelog.breaking_reasons,
                "llm_summary": llm_changelog.summary,
                "llm_pr_notes": llm_changelog.pr_changelog_snippet
            }
        except Exception as ex:
            clean_err = clean_error_message(ex)
            summary = f"Qualification error: {clean_err}"
            self._update_package_db(package_id, "-", summary, policy_status="ERROR")
            return {
                "package_id": package_id,
                "name": pkg_name,
                "current_version": current_ver,
                "upstream_version": "-",
                "candidate_version": None,
                "status": "ERROR",
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
    print(f"[RUNNING] Running Source Qualification Agent...")
    all_res = agent.qualify_all()
    for r in all_res:
        wf = r.get("workflow_status") or r.get("status")
        if wf == "UPDATE_FOUND":
            print(f"[UPDATE_FOUND] {r['package_id']} -> {r['candidate_version']}")
            print(f"  - LLM Verdict: {r['llm_verdict']}")
            print(f"  - Summary: {r['llm_summary']}")
        else:
            print(f"[{wf}] {r['package_id']}: {r.get('summary')}")
