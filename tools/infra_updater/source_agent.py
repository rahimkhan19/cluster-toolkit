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

For every package, the same pipeline runs regardless of the source type:
  fetch (fetchers.py) -> select (selector.py) -> gates -> record candidate.

Gates, in order: newer than deployed, production GA, snooze/block policy, learned rules,
open PR already tracking the version, artifact liveness.
"""

import datetime
import json
import os
import sys
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any, Callable, Dict, List, Optional, Tuple

from packaging.specifiers import InvalidSpecifier, SpecifierSet

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
if BASE_DIR not in sys.path:
    sys.path.insert(0, BASE_DIR)
from config import get_config
from datastore import BaseDataStore as DataStore, get_datastore
import fetchers
import http_client
from llm_client import clean_error_message, get_llm_client
import policy
from prompts import render_prompt
from selector import ReleaseSelector, normalize_version
from statuses import POLICY_STATUSES, PR_TRACKED_CANDIDATE_STATUSES, CandidateStatus, PackageStatus
from versions import clean_version_str, is_version_greater, parse_semver

CONFIG = get_config()

Result = Dict[str, Any]
Selected = Dict[str, Any]


class UpfrontRuleChecker:
    """Evaluates candidate versions against learned BLOCK rules, optionally scoped to one blueprint."""

    def __init__(self, store: Optional[DataStore] = None):
        self.store = store or get_datastore()

    @staticmethod
    def _matches(constraint: str, version: str) -> bool:
        """PEP 440 specifier (e.g. '>=2.0,<3') or a bare version for an exact match. Empty never matches."""
        constraint = (constraint or "").strip()
        if not constraint:
            return False
        try:
            parsed = parse_semver(version)
            return bool(parsed and parsed in SpecifierSet(constraint))
        except InvalidSpecifier:
            return clean_version_str(constraint.lstrip("=")) == clean_version_str(version)

    def check_version(
        self, package_id: str, version: str, blueprint_path: Optional[str] = None
    ) -> Tuple[bool, Optional[Dict[str, Any]]]:
        """Returns (is_blocked, matched_rule)."""
        for rule in self.store.list_rules(package_id):
            if rule.get("action", "BLOCK") != "BLOCK":
                continue
            scope = rule.get("scope") or {}
            if isinstance(scope, str):
                try:
                    scope = json.loads(scope)
                except json.JSONDecodeError:
                    scope = {}
            if blueprint_path and scope.get("blueprint", "*") not in ("*", blueprint_path):
                continue
            if self._matches(rule.get("version_constraint", ""), version):
                return True, rule
        return False, None


def _keep_policy(status: str) -> str:
    """Nothing new upstream: a snooze/block stays in place, everything else is up to date."""
    return status if status in POLICY_STATUSES else PackageStatus.UP_TO_DATE


class SourceQualificationAgent:
    """Runs the shared fetch -> select -> gates pipeline for each registered package."""

    def __init__(self, store: Optional[DataStore] = None, model: Optional[str] = None, use_llm: bool = True):
        self.store = store or get_datastore()
        self.llm = get_llm_client(model or CONFIG.llm.model) if use_llm else None
        self.ctx = fetchers.FetchContext(self.llm, CONFIG.github_token)
        self.selector = ReleaseSelector(self.llm, CONFIG.qualification.max_candidates)
        self.rule_checker = UpfrontRuleChecker(self.store)

    # ------------------------------------------------------------------ results

    def _result(self, pkg: Dict[str, Any], status: str, summary: str, upstream: str = "-",
                candidate_version: Optional[str] = None, persist: bool = True,
                db_extra: Optional[Dict[str, Any]] = None, **extra) -> Result:
        """Persists the package's qualification outcome and returns the result row."""
        if persist:
            self.store.update_package(pkg["package_id"], {
                "upstream_version": upstream, "qualification_summary": summary, "status": status, **(db_extra or {}),
            })
        return {
            "package_id": pkg["package_id"], "name": pkg.get("name", ""), "current_version": pkg.get("current_version", ""),
            "upstream_version": upstream, "candidate_version": candidate_version, "status": status, "summary": summary,
            **extra,
        }

    def summarize_release_notes(self, package_id: str, version: str, notes: str) -> str:
        """1-2 sentence LLM summary of upstream release notes (short notes are kept as-is)."""
        default = f"Qualified upstream GA release {version}."
        notes = (notes or "").strip()
        if not notes:
            return default
        if len(notes) < 40 or not self.llm:
            return notes[:200]
        try:
            text = self.llm.generate_text(render_prompt(
                "release_summary", package_id=package_id, version=version, release_notes=notes[:4000]))
            return text.replace("\n", " ") or default
        except Exception as ex:  # pylint: disable=broad-except
            print(f"[WARN] Failed to summarize release notes for {package_id}: {clean_error_message(ex)}")
            return notes[:200]

    # ------------------------------------------------------------------ gates
    # Each gate returns a result to stop qualification, or None to continue.

    def _gate_newer(self, pkg, version, sel) -> Optional[Result]:
        current = pkg.get("current_version", "")
        if is_version_greater(version, current):
            return None
        return self._result(pkg, _keep_policy(pkg.get("status")),
                            f"Deployed version {current} is up to date (latest upstream GA: {version}).", upstream=version)

    def _gate_ga(self, pkg, version, sel) -> Optional[Result]:
        if sel.get("is_ga", True):
            return None
        return self._result(pkg, _keep_policy(pkg.get("status")),
                            f"Upstream release {version} discarded as non-GA: {sel.get('reasoning', '')}", upstream=version)

    def _gate_policy(self, pkg, version, sel) -> Optional[Result]:
        hold = policy.policy_hold(pkg, version)
        return self._result(pkg, hold[0], hold[1], upstream=version) if hold else None

    def _gate_rules(self, pkg, version, sel) -> Optional[Result]:
        blocked, rule = self.rule_checker.check_version(pkg["package_id"], version)
        if not blocked:
            return None
        # blocked_version lets a newer upstream release be evaluated again.
        return self._result(pkg, PackageStatus.BLOCKED,
                            f"Version {version} blocked by rule '{rule['rule_id']}': {rule.get('reason', '')}",
                            upstream=version, db_extra={"blocked_version": version},
                            rule_id=rule["rule_id"], reason=rule.get("reason"))

    def _gate_open_pr(self, pkg, version, sel) -> Optional[Result]:
        """An open PR already tracks this version: keep it (and its test state) instead of re-creating it."""
        tracked = next((c for c in self.store.list_candidates(package_id=pkg["package_id"])
                        if c.get("status") in PR_TRACKED_CANDIDATE_STATUSES and c.get("version") == version), None)
        if not tracked:
            return None
        self.store.update_package(pkg["package_id"], {"upstream_version": version})
        return self._result(pkg, tracked["status"], f"Update {version} already has an open PR ({tracked.get('pr_url')}).",
                            upstream=version, candidate_version=version, persist=False,
                            candidate_id=tracked["candidate_id"])

    GATES: Tuple[Callable, ...] = (_gate_newer, _gate_ga, _gate_policy, _gate_rules, _gate_open_pr)

    # ------------------------------------------------------------------ pipeline

    def _record_candidate(self, pkg: Dict[str, Any], version: str, sel: Selected) -> Result:
        package_id = pkg["package_id"]
        cand_summary = self.summarize_release_notes(package_id, version, sel.get("notes"))
        if sel["method"] == "fallback":
            cand_summary += " (not LLM-verified)"
        candidate_id = f"cand-{uuid.uuid4().hex[:8]}"
        # Replace stale UPDATE_FOUND candidates; never drop merged, held, or PR-tracked ones.
        self.store.delete_candidates(package_id, exclude_status=(
            CandidateStatus.MERGED, *POLICY_STATUSES, *PR_TRACKED_CANDIDATE_STATUSES))
        self.store.save_candidate({
            "candidate_id": candidate_id,
            "package_id": package_id,
            "version": version,
            "current_version": pkg.get("current_version", ""),
            "download_url": sel.get("url"),
            "checksum": sel.get("sha256"),
            "selection_method": sel["method"],
            "pr_url": None,
            "build_url": None,
            "status": CandidateStatus.UPDATE_FOUND,
            "summary": cand_summary,
            "created_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        })
        return self._result(pkg, PackageStatus.UPDATE_FOUND, f"Update found ({version}): {cand_summary}",
                            upstream=version, candidate_version=version, candidate_id=candidate_id,
                            download_url=sel.get("url"), selection_method=sel["method"])

    def qualify_package(self, package_id: str) -> Result:
        pkg = self.store.get_package(package_id)
        if not pkg:
            return {"package_id": package_id, "current_version": "", "status": PackageStatus.ERROR,
                    "summary": f"Package {package_id} not found in database"}
        pkg = policy.release_expired_snooze(self.store, pkg)
        if pkg.get("status") == PackageStatus.OBSOLETE:
            return self._result(pkg, PackageStatus.OBSOLETE, "Package is currently marked OBSOLETE.", persist=False)

        try:
            sel = self.selector.select(pkg, fetchers.fetch_candidates(pkg, self.ctx))
            if not sel:
                return self._result(pkg, _keep_policy(pkg.get("status")),
                                    f"No GA release newer than the deployed version ({pkg.get('current_version')}) upstream.")
            version = normalize_version(sel["version"], pkg.get("current_version"))
            for gate in self.GATES:
                res = gate(self, pkg, version, sel)
                if res:
                    return res
            sel = fetchers.resolve_candidate(pkg, sel, self.ctx)
            if not http_client.is_url_live(sel.get("url")):
                return self._result(pkg, PackageStatus.UNREACHABLE,
                                    f"Release {version} download URL unreachable: {sel.get('url')}", upstream=version)
            return self._record_candidate(pkg, version, sel)
        except Exception as ex:  # pylint: disable=broad-except
            err = clean_error_message(ex)
            return self._result(pkg, PackageStatus.ERROR, f"Qualification error: {err}", error=err)

    def qualify_all(self) -> List[Result]:
        """Qualifies every package in parallel; results are returned in registry order."""
        package_ids = [p["package_id"] for p in self.store.list_packages()]
        total = len(package_ids)
        results: Dict[str, Result] = {}
        with ThreadPoolExecutor(max_workers=CONFIG.qualification.max_workers) as pool:
            futures = {pool.submit(self.qualify_package, pid): pid for pid in package_ids}
            for i, fut in enumerate(as_completed(futures), 1):
                res = results[futures[fut]] = fut.result()
                print(f"[{i}/{total}] {res['package_id']:<18} -> {res.get('status')} | "
                      f"Upstream: {res.get('upstream_version') or '-'} | Target: {res.get('candidate_version') or '-'}",
                      flush=True)
        return [results[pid] for pid in package_ids]


if __name__ == "__main__":
    for r in SourceQualificationAgent().qualify_all():
        print(f"[{r['status']}] {r['package_id']}: {r.get('summary')}")
