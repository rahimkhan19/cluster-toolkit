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
Release selection shared by every source type (hybrid: deterministic prefilter + LLM pick).

  1. prefilter   drop pre-releases and versions older than the deployed one; newest first.
  2. select      one candidate -> taken as-is; otherwise the LLM picks (prompts/select_release.txt).
                 Without an LLM, with source_options.selector == "deterministic", or when the LLM
                 call fails / answers with a version that is not in the list, the newest
                 prefiltered candidate is used and marked as not LLM-verified.
"""

import functools
import re
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field

from fetchers import Candidate, describe
from llm_client import clean_error_message
from prompts import render_prompt
from versions import is_version_greater, match_v_prefix, parse_semver

PRERELEASE_RE = re.compile(
    r"(?i)(?:^|[-_.+~])(?:rc|beta|alpha|preview|pre|dev|test|nightly|snapshot|draft|dirty|unverified)\d*(?=$|[-_.+~])"
)

# Source-specific guidance appended to the generic prompt (overridable per package via prompt_hints).
DEFAULT_RULES: Dict[str, str] = {
    "archive_scraper": "Candidates are installer links scraped from a downloads page: prefer the full standalone "
                       "Linux installer over network, patch or driver-only packages.",
    "docker_hub": "Versions are complete image tags (e.g. 13.4.2-base-ubuntu24.04); copy the full tag.",
    "apt_repository": "Versions are Debian package versions; copy them including any epoch prefix (e.g. '1:').",
}


class ReleaseSelection(BaseModel):
    version: str = Field(description="The selected candidate's version, copied exactly from the list")
    is_production_ga: bool = Field(description="True if the selected release is a production GA release")
    reasoning: str = Field(description="One sentence on why this release was selected")


def is_prerelease(cand: Candidate) -> bool:
    version = cand.get("version", "")
    if cand.get("prerelease") or PRERELEASE_RE.search(version):
        return True
    parsed = parse_semver(version)
    return bool(parsed and (parsed.is_prerelease or parsed.is_devrelease))


def _cmp(a: Candidate, b: Candidate) -> int:
    if is_version_greater(a["version"], b["version"]):
        return -1
    return 1 if is_version_greater(b["version"], a["version"]) else 0


def prefilter(cands: List[Candidate], current: Optional[str], limit: int = 25) -> List[Candidate]:
    """GA-looking, parseable candidates >= the deployed version, deduplicated, newest first."""
    current_semver = parse_semver(current or "")
    seen, kept = set(), []
    for c in cands:
        version = (c.get("version") or "").strip()
        if not version or version in seen or is_prerelease(c):
            continue
        parsed = parse_semver(version)
        if not parsed or (current_semver and parsed < current_semver):
            continue
        seen.add(version)
        kept.append({**c, "version": version})
    return sorted(kept, key=functools.cmp_to_key(_cmp))[:limit]


def _core(version: str) -> str:
    return re.sub(r"^(?:\d+:)?[vV]?", "", version.strip())


def normalize_version(version: str, current: Optional[str]) -> str:
    """Aligns the 'v' prefix with the deployed version's style (v1.2.3 vs 1.2.3)."""
    return match_v_prefix(current or "", version)


def _numbered(rules: List[str], start: int) -> str:
    return "\n".join(f"{i}. {r}" for i, r in enumerate((r for r in rules if r), start))


class ReleaseSelector:
    """Picks the newest production GA release out of fetched candidates."""

    def __init__(self, llm=None, max_candidates: int = 25):
        self.llm = llm
        self.max_candidates = max_candidates

    def select(self, pkg: Dict[str, Any], cands: List[Candidate]) -> Optional[Dict[str, Any]]:
        """Returns the chosen candidate plus is_ga / method / reasoning, or None if nothing qualifies."""
        current = pkg.get("current_version")
        pool = prefilter(cands, current, self.max_candidates)
        if not pool:
            return None
        newest = pool[0]
        opts = pkg.get("source_options") or {}

        if len(pool) == 1:
            method = "llm" if newest.get("verified") else "single"
            return {**newest, "is_ga": True, "method": method, "reasoning": newest.get("notes", "")}
        if opts.get("selector") == "deterministic" or not self.llm:
            return {**newest, "is_ga": True, "method": "deterministic", "reasoning": "Newest GA-looking release."}

        prompt = render_prompt(
            "select_release", package_id=pkg["package_id"], source_type=pkg.get("upstream_type"),
            source_url=pkg.get("source_url"), current_version=current or "-", candidates_json=describe(pool),
            extra_rules=_numbered([DEFAULT_RULES.get(pkg.get("upstream_type"), ""), opts.get("prompt_hints", "")], 5),
        )
        try:
            answer = self.llm.generate_json(prompt, ReleaseSelection)
            picked = next((c for c in pool if c["version"] == answer.version.strip()), None) or \
                next((c for c in pool if _core(c["version"]) == _core(answer.version)), None)
            if picked:
                return {**picked, "is_ga": answer.is_production_ga, "method": "llm", "reasoning": answer.reasoning}
            reason = f"LLM answered '{answer.version}', which is not a candidate"
        except Exception as ex:  # pylint: disable=broad-except
            reason = f"LLM selection failed: {clean_error_message(ex)}"
        print(f"[WARN] {pkg['package_id']}: {reason}; using newest release {newest['version']} (not LLM-verified).")
        return {**newest, "is_ga": True, "method": "fallback", "reasoning": f"{reason} (not LLM-verified)."}
