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
JSON DataStore for Cluster Toolkit Automated Dependency Management.

Manages the local state JSON file (updater_state.json), providing thread-safe,
atomic CRUD operations for:
- packages (Canonical registry, long-term policy status, and embedded blueprint instances)
- candidate_updates (Lifecycle of candidate versions: UPDATE_FOUND, TESTING, READY_FOR_REVIEW, etc.)
- learned_rules (Upfront learned rule engine constraints)
- audit_runs (Operational execution telemetry)

Serves as the unified abstraction layer that seamlessly adapts to Google Cloud
Firestore / Firebase in Phase 2.
"""

import copy
import datetime
import json
import os
import tempfile
import threading
from typing import Any, Dict, List, Optional

from tools.infra_updater.config import get_config

BASE_DIR = os.path.dirname(os.path.abspath(__file__))

def get_default_json_path() -> str:
    return os.path.join(BASE_DIR, "updater_state.json")

class DataStore:
    """Thread-safe, atomic JSON data store for dependency registry and lifecycle state."""

    def __init__(self, json_path: Optional[str] = None):
        self.json_path = json_path or os.environ.get("UPDATER_STATE_JSON", get_default_json_path())
        self._lock = threading.Lock()
        self._ensure_initialized()

    def _ensure_initialized(self):
        if not os.path.exists(self.json_path):
            self.init_from_seed()

    def _read_data(self) -> Dict[str, Any]:
        """Reads and returns state dictionary from JSON file."""
        if not os.path.exists(self.json_path):
            return {
                "packages": {},
                "candidate_updates": {},
                "learned_rules": [],
                "audit_runs": []
            }
        try:
            with open(self.json_path, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception as ex:
            print(f"[ERROR] Failed to read {self.json_path}: {ex}")
            return {
                "packages": {},
                "candidate_updates": {},
                "learned_rules": [],
                "audit_runs": []
            }

    def _write_data(self, data: Dict[str, Any]) -> None:
        """Atomically writes data to JSON file via a temporary file."""
        dir_name = os.path.dirname(self.json_path)
        os.makedirs(dir_name, exist_ok=True)
        temp_fd, temp_path = tempfile.mkstemp(dir=dir_name, prefix="updater_state_", suffix=".tmp")
        try:
            with os.fdopen(temp_fd, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=2, ensure_ascii=False)
            os.replace(temp_path, self.json_path)
        except Exception:
            if os.path.exists(temp_path):
                os.remove(temp_path)
            raise

    # --------------------------------------------------------------------------
    # Packages (Canonical Registry)
    # --------------------------------------------------------------------------

    def get_package(self, package_id: str) -> Optional[Dict[str, Any]]:
        """Retrieves a single package definition by its package_id."""
        with self._lock:
            data = self._read_data()
            pkg = data.get("packages", {}).get(package_id)
            return copy.deepcopy(pkg) if pkg else None

    def list_packages(self) -> List[Dict[str, Any]]:
        """Returns all registered packages ordered by package_id."""
        with self._lock:
            data = self._read_data()
            pkgs = list(data.get("packages", {}).values())
            pkgs.sort(key=lambda x: x.get("package_id", ""))
            return copy.deepcopy(pkgs)

    def update_package(self, package_id: str, updates: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        """Updates specific fields on a package record."""
        with self._lock:
            data = self._read_data()
            packages = data.setdefault("packages", {})
            if package_id not in packages:
                return None
            pkg = packages[package_id]
            for k, v in updates.items():
                pkg[k] = v
            pkg["updated_at"] = datetime.datetime.now(datetime.timezone.utc).isoformat()
            self._write_data(data)
            return copy.deepcopy(pkg)

    # --------------------------------------------------------------------------
    # Blueprints
    # --------------------------------------------------------------------------

    def get_blueprints_for_package(self, package_id: str) -> List[Dict[str, Any]]:
        """Returns all blueprint instances mapped to a package."""
        pkg = self.get_package(package_id)
        if not pkg:
            return []
        return copy.deepcopy(pkg.get("blueprints", []))

    def list_all_blueprints(self) -> List[Dict[str, Any]]:
        """Returns a flat list of all blueprint instances across all packages."""
        with self._lock:
            data = self._read_data()
            all_bps = []
            for pkg_id, pkg in data.get("packages", {}).items():
                for bp in pkg.get("blueprints", []):
                    item = copy.deepcopy(bp)
                    item["package_id"] = pkg_id
                    all_bps.append(item)
            all_bps.sort(key=lambda x: x.get("instance_id", ""))
            return all_bps

    # --------------------------------------------------------------------------
    # Candidate Updates Lifecycle
    # --------------------------------------------------------------------------

    def get_candidate(self, candidate_id: str) -> Optional[Dict[str, Any]]:
        """Returns a single candidate update by candidate_id."""
        with self._lock:
            data = self._read_data()
            cand = data.get("candidate_updates", {}).get(candidate_id)
            return copy.deepcopy(cand) if cand else None

    def list_candidates(
        self,
        package_id: Optional[str] = None,
        status: Optional[str] = None
    ) -> List[Dict[str, Any]]:
        """Returns candidate updates, optionally filtered by package_id and/or workflow status."""
        with self._lock:
            data = self._read_data()
            cands = list(data.get("candidate_updates", {}).values())
            if package_id:
                cands = [c for c in cands if c.get("package_id") == package_id]
            if status:
                cands = [c for c in cands if c.get("status") == status]
            cands.sort(key=lambda x: x.get("created_at", ""), reverse=True)
            return copy.deepcopy(cands)

    def get_active_candidate(self, package_id: str) -> Optional[Dict[str, Any]]:
        """Returns the current active candidate (non-MERGED, non-CANCELLED) for a package."""
        cands = self.list_candidates(package_id=package_id)
        for c in cands:
            if c.get("status") not in ("MERGED", "CANCELLED", "SUPERSEDED"):
                return c
        return None

    def save_candidate(self, candidate: Dict[str, Any]) -> str:
        """Inserts or updates a candidate update record."""
        cand_id = candidate.get("candidate_id")
        if not cand_id:
            import uuid
            cand_id = f"cand-{str(uuid.uuid4())[:8]}"
            candidate["candidate_id"] = cand_id

        if "created_at" not in candidate:
            candidate["created_at"] = datetime.datetime.now(datetime.timezone.utc).isoformat()

        with self._lock:
            data = self._read_data()
            cands = data.setdefault("candidate_updates", {})
            cands[cand_id] = copy.deepcopy(candidate)
            self._write_data(data)
            return cand_id

    def update_candidate(self, candidate_id: str, updates: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        """Updates fields on an existing candidate update record."""
        with self._lock:
            data = self._read_data()
            cands = data.setdefault("candidate_updates", {})
            if candidate_id not in cands:
                return None
            cand = cands[candidate_id]
            for k, v in updates.items():
                cand[k] = v
            self._write_data(data)
            return copy.deepcopy(cand)

    def delete_candidates(self, package_id: str, exclude_status: Optional[str] = "MERGED") -> int:
        """Deletes candidate updates for a package (except those matching exclude_status)."""
        with self._lock:
            data = self._read_data()
            cands = data.setdefault("candidate_updates", {})
            to_delete = [
                cid for cid, c in cands.items()
                if c.get("package_id") == package_id and (exclude_status is None or c.get("status") != exclude_status)
            ]
            for cid in to_delete:
                del cands[cid]
            if to_delete:
                self._write_data(data)
            return len(to_delete)

    # --------------------------------------------------------------------------
    # Learned Rules
    # --------------------------------------------------------------------------

    def list_rules(self, package_id: Optional[str] = None) -> List[Dict[str, Any]]:
        """Returns learned rules, optionally filtered by package_id."""
        with self._lock:
            data = self._read_data()
            rules = data.get("learned_rules", [])
            if package_id:
                rules = [r for r in rules if r.get("package_id") == package_id or r.get("package_id") == "*"]
            return copy.deepcopy(rules)

    def add_rule(self, rule_data: Dict[str, Any]) -> str:
        """Appends a new rule to learned_rules."""
        rule_id = rule_data.get("rule_id")
        if not rule_id:
            import uuid
            rule_id = f"rule-{str(uuid.uuid4())[:8]}"
            rule_data["rule_id"] = rule_id
        if "created_at" not in rule_data:
            rule_data["created_at"] = datetime.datetime.now(datetime.timezone.utc).isoformat()

        with self._lock:
            data = self._read_data()
            rules = data.setdefault("learned_rules", [])
            # Replace existing rule with same ID if present
            rules = [r for r in rules if r.get("rule_id") != rule_id]
            rules.append(copy.deepcopy(rule_data))
            data["learned_rules"] = rules
            self._write_data(data)
            return rule_id

    def delete_rule(self, rule_id: str) -> bool:
        """Deletes a rule by rule_id."""
        with self._lock:
            data = self._read_data()
            rules = data.setdefault("learned_rules", [])
            orig_len = len(rules)
            rules = [r for r in rules if r.get("rule_id") != rule_id]
            data["learned_rules"] = rules
            if len(rules) != orig_len:
                self._write_data(data)
                return True
            return False

    # --------------------------------------------------------------------------
    # Audit Runs
    # --------------------------------------------------------------------------

    def record_audit_run(self, run_data: Dict[str, Any]) -> str:
        """Records an execution telemetry audit run."""
        run_id = run_data.get("run_id")
        if not run_id:
            import uuid
            run_id = f"run-{datetime.datetime.now().strftime('%Y%m%d_%H%M%S')}-{str(uuid.uuid4())[:4]}"
            run_data["run_id"] = run_id
        if "started_at" not in run_data:
            run_data["started_at"] = datetime.datetime.now(datetime.timezone.utc).isoformat()

        with self._lock:
            data = self._read_data()
            runs = data.setdefault("audit_runs", [])
            runs.insert(0, copy.deepcopy(run_data))
            # Keep max 50 recent runs
            data["audit_runs"] = runs[:50]
            self._write_data(data)
            return run_id

    def list_audit_runs(self, limit: int = 10) -> List[Dict[str, Any]]:
        """Returns recent audit runs up to limit."""
        with self._lock:
            data = self._read_data()
            runs = data.get("audit_runs", [])
            return copy.deepcopy(runs[:limit])

    # --------------------------------------------------------------------------
    # Dashboard Consolidated View
    # --------------------------------------------------------------------------

    def get_full_state(self) -> Dict[str, Any]:
        """Consolidates packages, active candidates, blueprints, rules, and telemetry for the API / UI."""
        with self._lock:
            data = self._read_data()
            packages_map = data.get("packages", {})
            candidates_map = data.get("candidate_updates", {})
            rules = data.get("learned_rules", [])
            audit_runs = data.get("audit_runs", [])

            # Index active candidate updates by package_id
            active_cands_by_pkg = {}
            for cand in candidates_map.values():
                pid = cand.get("package_id")
                if cand.get("status") not in ("MERGED", "CANCELLED", "SUPERSEDED"):
                    active_cands_by_pkg[pid] = cand

            enriched_packages = []
            all_blueprints = []

            for pid, pkg in sorted(packages_map.items()):
                cand = active_cands_by_pkg.get(pid)
                bps = pkg.get("blueprints", [])
                for bp in bps:
                    bp_copy = copy.deepcopy(bp)
                    bp_copy["package_id"] = pid
                    all_blueprints.append(bp_copy)

                enriched_pkg = {
                    "package_id": pid,
                    "name": pkg.get("name", ""),
                    "current_version": pkg.get("current_version", ""),
                    "upstream_version": pkg.get("upstream_version") or "-",
                    "source_url": pkg.get("source_url", ""),
                    "upstream_type": pkg.get("upstream_type", "generic"),
                    "status": pkg.get("status", "REGISTERED"),
                    "qualification_summary": pkg.get("qualification_summary") or "Monitored baseline.",
                    "snooze_until": pkg.get("snooze_until"),
                    "updated_at": pkg.get("updated_at", ""),
                    "candidate_id": cand.get("candidate_id") if cand else None,
                    "candidate_version": cand.get("version") if cand else None,
                    "candidate_status": cand.get("status") if cand else None,
                    "pr_url": cand.get("pr_url") if cand else None,
                    "build_url": cand.get("build_url") if cand else None,
                    "candidate_summary": cand.get("summary") if cand else None,
                    "blueprints_count": len(bps)
                }
                enriched_packages.append(enriched_pkg)

            return {
                "packages": enriched_packages,
                "blueprints": all_blueprints,
                "rules": rules,
                "audit_runs": audit_runs
            }

    # --------------------------------------------------------------------------
    # Seeding / Migration
    # --------------------------------------------------------------------------

    def init_from_seed(self, force: bool = False):
        """Initializes updater_state.json with canonical Cluster Toolkit package definitions and blueprint maps."""
        if os.path.exists(self.json_path) and not force:
            return

        from tools.infra_updater.init_db import get_seed_data
        seed_data = get_seed_data()
        self._write_data(seed_data)
        print(f"[SUCCESS] Initialized JSON state store at {self.json_path}")


_GLOBAL_STORE: Optional[DataStore] = None


def get_datastore(json_path: Optional[str] = None) -> DataStore:
    """Returns the process-wide singleton DataStore."""
    global _GLOBAL_STORE
    if _GLOBAL_STORE is None or (json_path and _GLOBAL_STORE.json_path != json_path):
        _GLOBAL_STORE = DataStore(json_path)
    return _GLOBAL_STORE
