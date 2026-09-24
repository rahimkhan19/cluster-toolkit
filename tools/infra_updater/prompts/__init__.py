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
Central Prompt Management Module for Cluster Toolkit Automated Infrastructure Updater.

All LLM prompts are externalized into template files under tools/infra_updater/prompts/
and loaded dynamically rather than being hardcoded across pipeline modules.
"""

import os
from functools import lru_cache
from typing import Any

PROMPTS_DIR = os.path.dirname(os.path.abspath(__file__))


@lru_cache(maxsize=32)
def load_prompt(name: str) -> str:
    """Loads a prompt template from tools/infra_updater/prompts/."""
    candidates = [
        os.path.join(PROMPTS_DIR, f"{name}.txt"),
        os.path.join(PROMPTS_DIR, f"{name}.md"),
        os.path.join(PROMPTS_DIR, name)
    ]
    for p in candidates:
        if os.path.exists(p):
            with open(p, "r", encoding="utf-8") as f:
                return f.read().strip()
    raise FileNotFoundError(f"Prompt template '{name}' not found in {PROMPTS_DIR}")


def get_archive_scraper_prompt(page_url: str, package_id: str, target_arch: str, discovered_urls_json: str) -> str:
    template = load_prompt("archive_scraper")
    return template.format(
        page_url=page_url,
        package_id=package_id,
        target_arch=target_arch,
        discovered_urls_json=discovered_urls_json
    )


def get_manifest_regex_prompt(package_id: str, manifest_url: str, manifest_content: str) -> str:
    template = load_prompt("manifest_regex")
    return template.format(
        package_id=package_id,
        manifest_url=manifest_url,
        manifest_content=manifest_content
    )


def get_github_release_prompt(source_url: str, package_id: str, candidates_summary_json: str) -> str:
    template = load_prompt("github_release")
    return template.format(
        source_url=source_url,
        package_id=package_id,
        candidates_summary_json=candidates_summary_json
    )


def get_apt_repo_prompt(package_id: str, target_pkg: str, repo_source: str, available_versions_json: str) -> str:
    template = load_prompt("apt_repo")
    return template.format(
        package_id=package_id,
        target_pkg=target_pkg,
        repo_source=repo_source,
        available_versions_json=available_versions_json
    )


def get_compute_image_prompt(family: str, project: str, package_id: str, image_metadata_json: str) -> str:
    template = load_prompt("compute_image")
    return template.format(
        family=family,
        project=project,
        package_id=package_id,
        image_metadata_json=image_metadata_json
    )


def get_docker_hub_prompt(package_id: str, repository: str, current_version: str, candidate_tags_json: str) -> str:
    template = load_prompt("docker_hub")
    return template.format(
        package_id=package_id,
        repository=repository,
        current_version=current_version,
        candidate_tags_json=candidate_tags_json
    )


def get_release_summary_prompt(package_id: str, version: str, release_notes: str) -> str:
    template = load_prompt("release_summary")
    return template.format(
        package_id=package_id,
        version=version,
        release_notes=release_notes
    )

