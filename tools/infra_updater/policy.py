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
Version-aware snooze / block policy, shared by the CLI, the dashboard server,
qualification (source_agent) and apply (code_modifier).

A snooze or block covers one version; a strictly newer upstream version is not held.
"""

import datetime
import functools
from typing import Any, Dict, Optional, Tuple

from config import get_config
from statuses import (
    ACTIVE_CANDIDATE_STATUSES, POLICY_STATUSES, CandidateStatus, PackageStatus, derive_package_status,
)
from versions import is_version_greater


def _now() -> datetime.datetime:
    return datetime.datetime.now(datetime.timezone.utc)


def _error(package_id: str) -> Dict[str, Any]:
    return {"status": "ERROR", "message": f"Package '{package_id}' not found.", "package_id": package_id}


def _cmp_version(a: Dict[str, Any], b: Dict[str, Any]) -> int:
    va, vb = a.get("version") or "", b.get("version") or ""
    return 1 if is_version_greater(va, vb) else (-1 if is_version_greater(vb, va) else 0)


def resolve_version(store, pkg: Dict[str, Any], version: Optional[str] = None) -> str:
    """Explicit version, else the active candidate's version, else the known upstream/current version."""
    if version:
        return version
    cand = store.get_active_candidate(pkg["package_id"])
    if cand:
        return cand["version"]
    upstream = pkg.get("upstream_version")
    return upstream if upstream and upstream != "-" else pkg.get("current_version", "")


def _mark_candidate(store, pkg: Dict[str, Any], version: str, status: str, summary: str) -> None:
    """Sets the candidate for `version` to a policy status, creating a record if none exists."""
    cand = next((c for c in store.list_candidates(package_id=pkg["package_id"]) if c.get("version") == version), None)
    if cand:
        store.update_candidate(cand["candidate_id"], {"status": status, "summary": summary})
        return
    store.save_candidate({
        "package_id": pkg["package_id"],
        "version": version,
        "current_version": pkg.get("current_version"),
        "download_url": pkg.get("source_url"),
        "status": status,
        "summary": summary,
    })


def snooze(store, package_id: str, days: Optional[int] = None, version: Optional[str] = None) -> Dict[str, Any]:
    pkg = store.get_package(package_id)
    if not pkg:
        return _error(package_id)
    days = int(days or get_config().policy.default_snooze_days)
    version = resolve_version(store, pkg, version)
    until = (_now() + datetime.timedelta(days=days)).isoformat()
    summary = f"Snoozed version {version} for {days} days (until {until[:10]})."
    store.update_package(package_id, {
        "status": PackageStatus.SNOOZED, "snooze_until": until,
        "snoozed_version": version, "qualification_summary": summary,
    })
    _mark_candidate(store, pkg, version, CandidateStatus.SNOOZED, summary)
    return {"status": "SUCCESS", "message": summary, "package_id": package_id, "version": version, "snooze_until": until}


def block(store, package_id: str, version: Optional[str] = None) -> Dict[str, Any]:
    pkg = store.get_package(package_id)
    if not pkg:
        return _error(package_id)
    version = resolve_version(store, pkg, version)
    summary = f"Blocked version {version} (manual unblock required from dashboard)."
    store.update_package(package_id, {
        "status": PackageStatus.BLOCKED, "blocked_version": version, "qualification_summary": summary,
    })
    _mark_candidate(store, pkg, version, CandidateStatus.BLOCKED, summary)
    return {"status": "SUCCESS", "message": summary, "package_id": package_id, "version": version}


def unblock(store, package_id: str) -> Dict[str, Any]:
    """
    Clears snooze/block. If another update is already pending, held candidates are dropped;
    otherwise the highest held version becomes UPDATE_FOUND again. The package status is
    derived from the remaining candidates.
    """
    pkg = store.get_package(package_id)
    if not pkg:
        return _error(package_id)
    cands = store.list_candidates(package_id=package_id)
    remaining = [c["status"] for c in cands if c.get("status") in ACTIVE_CANDIDATE_STATUSES]
    held = [c for c in cands if c.get("status") in POLICY_STATUSES]
    # Revive the highest held version, unless another update is already pending.
    revive = None if remaining else max(held, key=functools.cmp_to_key(_cmp_version), default=None)
    for c in held:
        if c is revive:
            store.update_candidate(c["candidate_id"], {
                "status": CandidateStatus.UPDATE_FOUND, "summary": f"Unblocked candidate ({c.get('version')}).",
            })
            remaining.append(CandidateStatus.UPDATE_FOUND)
        else:
            store.delete_candidate(c["candidate_id"])

    summary = "Unblocked manually. Ready for qualification."
    store.update_package(package_id, {
        "status": derive_package_status(remaining) or PackageStatus.REGISTERED,
        "snooze_until": None, "snoozed_version": None, "blocked_version": None,
        "qualification_summary": summary,
    })
    return {"status": "SUCCESS", "message": summary, "package_id": package_id}


def is_snooze_expired(pkg: Dict[str, Any]) -> bool:
    until = pkg.get("snooze_until")
    return bool(until) and str(until) <= _now().isoformat()


def release_expired_snooze(store, pkg: Dict[str, Any]) -> Dict[str, Any]:
    """Clears an expired snooze (resuming monitoring). Returns the up-to-date package."""
    if not is_snooze_expired(pkg):
        return pkg
    updates: Dict[str, Any] = {"snooze_until": None, "snoozed_version": None,
                               "qualification_summary": "Snooze expired. Monitoring resumed."}
    if pkg.get("status") == PackageStatus.SNOOZED:
        updates["status"] = PackageStatus.REGISTERED
    return store.update_package(pkg["package_id"], updates) or {**pkg, **updates}


def policy_hold(pkg: Dict[str, Any], version: str) -> Optional[Tuple[str, str]]:
    """(status, message) if an active snooze or block on the package covers `version`, else None."""
    snoozed = pkg.get("snoozed_version")
    if (snoozed or pkg.get("status") == PackageStatus.SNOOZED) and not is_snooze_expired(pkg):
        if not (snoozed and is_version_greater(version, snoozed)):
            until = pkg.get("snooze_until")
            return (PackageStatus.SNOOZED,
                    f"Package is SNOOZED for version {snoozed or version} until {str(until)[:10] if until else 'further notice'}.")
    blocked = pkg.get("blocked_version")
    if blocked or pkg.get("status") == PackageStatus.BLOCKED:
        if not (blocked and is_version_greater(version, blocked)):
            return (PackageStatus.BLOCKED,
                    f"Package is BLOCKED for version {blocked or version} (manual unblock required from dashboard).")
    return None
