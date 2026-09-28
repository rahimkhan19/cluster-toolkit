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
Unified DataStore Abstraction for Cluster Toolkit Automated Dependency Management.

Supports pluggable database backends:
1. Cloud Firestore (Firebase in Native Mode on GCP) via ADC or service account credentials.
2. Local thread-safe JSON datastore (updater_state.json) for offline/local development.

Both backends implement BaseDataStore, providing seamless, zero-breaking-change CRUD for:
- packages (Canonical registry, long-term policy status, and embedded blueprint instances)
- candidate_updates (Lifecycle of candidate versions: UPDATE_FOUND, READY_FOR_REVIEW, etc.)
- learned_rules (Upfront learned rule engine constraints)
- audit_runs (Operational execution telemetry)
"""

import abc
import copy
import datetime
import json
import os
import tempfile
import threading
import uuid
from typing import Any, Dict, List, Optional

from tools.infra_updater.config import get_config

BASE_DIR = os.path.dirname(os.path.abspath(__file__))


def get_default_json_path() -> str:
    return os.path.join(BASE_DIR, "updater_state.json")


# ==============================================================================
# Abstract Base DataStore Contract
# ==============================================================================

class BaseDataStore(abc.ABC):
    """Abstract interface defining all dependency management persistence operations."""

    # Packages
    @abc.abstractmethod
    def get_package(self, package_id: str) -> Optional[Dict[str, Any]]:
        pass

    @abc.abstractmethod
    def list_packages(self) -> List[Dict[str, Any]]:
        pass

    @abc.abstractmethod
    def update_package(self, package_id: str, updates: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        pass

    # Blueprints
    def get_blueprints_for_package(self, package_id: str) -> List[Dict[str, Any]]:
        pkg = self.get_package(package_id)
        if not pkg:
            return []
        return copy.deepcopy(pkg.get("blueprints", []))

    def list_all_blueprints(self) -> List[Dict[str, Any]]:
        pkgs = self.list_packages()
        all_bps = []
        for pkg in pkgs:
            pid = pkg.get("package_id")
            pname = pkg.get("name", pid)
            pver = pkg.get("current_version", "")
            for bp in pkg.get("blueprints", []):
                bp_copy = copy.deepcopy(bp)
                bp_copy["package_id"] = pid
                bp_copy["package_name"] = pname
                bp_copy["current_version"] = pver
                all_bps.append(bp_copy)
        return all_bps

    # Candidates
    @abc.abstractmethod
    def get_candidate(self, candidate_id: str) -> Optional[Dict[str, Any]]:
        pass

    @abc.abstractmethod
    def list_candidates(
        self, package_id: Optional[str] = None, status: Optional[str] = None
    ) -> List[Dict[str, Any]]:
        pass

    def get_active_candidate(self, package_id: str) -> Optional[Dict[str, Any]]:
        cands = self.list_candidates(package_id=package_id)
        for c in cands:
            if c.get("status") not in ("MERGED", "CANCELLED", "SUPERSEDED"):
                return c
        return None

    @abc.abstractmethod
    def save_candidate(self, candidate: Dict[str, Any]) -> str:
        pass

    @abc.abstractmethod
    def update_candidate(self, candidate_id: str, updates: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        pass

    @abc.abstractmethod
    def delete_candidates(self, package_id: Optional[str] = None, exclude_status: Optional[str] = "MERGED") -> int:
        pass

    # Learned Rules
    @abc.abstractmethod
    def list_rules(self, package_id: Optional[str] = None) -> List[Dict[str, Any]]:
        pass

    @abc.abstractmethod
    def add_rule(self, rule_data: Dict[str, Any]) -> str:
        pass

    @abc.abstractmethod
    def delete_rule(self, rule_id: str) -> bool:
        pass

    # Audit Runs
    @abc.abstractmethod
    def record_audit_run(self, run_data: Dict[str, Any]) -> str:
        pass

    @abc.abstractmethod
    def list_audit_runs(self, limit: int = 10) -> List[Dict[str, Any]]:
        pass

    # State Consolidation & Seeding
    def get_full_state(self) -> Dict[str, Any]:
        """Consolidates packages, active candidates, blueprints, rules, and telemetry for the API / UI."""
        packages_list = self.list_packages()
        candidates_list = self.list_candidates()
        rules = self.list_rules()
        audit_runs = self.list_audit_runs(limit=20)

        # Index active candidate updates by package_id
        active_cands_by_pkg = {}
        for cand in candidates_list:
            pid = cand.get("package_id")
            if cand.get("status") not in ("MERGED", "CANCELLED", "SUPERSEDED"):
                active_cands_by_pkg[pid] = cand

        enriched_packages = []
        all_blueprints = []

        for pkg in packages_list:
            pid = pkg.get("package_id")
            cand = active_cands_by_pkg.get(pid)
            bps = pkg.get("blueprints", [])
            for bp in bps:
                bp_copy = copy.deepcopy(bp)
                bp_copy["package_id"] = pid
                bp_copy["package_name"] = pkg.get("name", pid)
                bp_copy["current_version"] = pkg.get("current_version", "")
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

    @abc.abstractmethod
    def init_from_seed(self, force: bool = False):
        pass


# ==============================================================================
# Cloud Firestore (Firebase) DataStore Implementation
# ==============================================================================

class FirestoreDataStore(BaseDataStore):
    """
    Cloud Firestore (Firebase) DataStore implementation.
    Operates on Google Cloud Firestore in Native Mode using Application Default Credentials (ADC).
    """

    def __init__(self, project_id: Optional[str] = None, database_id: Optional[str] = None):
        cfg = get_config()
        self.project_id = project_id or cfg.database.project_id or "hpc-toolkit-dev"
        self.database_id = database_id or cfg.database.database_id or "automated-dependency-management-db"
        self._client = None
        self._ensure_initialized()

    @property
    def db(self):
        if self._client is None:
            from google.cloud import firestore
            self._client = firestore.Client(project=self.project_id, database=self.database_id)
        return self._client

    def _ensure_initialized(self):
        """Auto-seeds packages if the Firestore collection is empty."""
        try:
            coll = self.db.collection("packages")
            first_doc = next(coll.limit(1).stream(), None)
            if first_doc is None:
                print(f"[FirestoreDataStore] Initializing empty Firestore database '{self.database_id}' from seed data...")
                self.init_from_seed()
        except Exception as ex:
            print(f"[FirestoreDataStore] [WARN] Initialization check failed: {ex}")

    # Packages
    def get_package(self, package_id: str) -> Optional[Dict[str, Any]]:
        doc = self.db.collection("packages").document(package_id).get()
        return doc.to_dict() if doc.exists else None

    def list_packages(self) -> List[Dict[str, Any]]:
        docs = self.db.collection("packages").stream()
        pkgs = [d.to_dict() for d in docs]
        pkgs.sort(key=lambda x: x.get("package_id", ""))
        return pkgs

    def update_package(self, package_id: str, updates: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        doc_ref = self.db.collection("packages").document(package_id)
        doc = doc_ref.get()
        if not doc.exists:
            return None
        data = doc.to_dict()
        updates_with_ts = {
            **updates,
            "updated_at": datetime.datetime.now(datetime.timezone.utc).isoformat()
        }
        doc_ref.set(updates_with_ts, merge=True)
        data.update(updates_with_ts)
        return data

    # Candidates
    def get_candidate(self, candidate_id: str) -> Optional[Dict[str, Any]]:
        doc = self.db.collection("candidate_updates").document(candidate_id).get()
        return doc.to_dict() if doc.exists else None

    def list_candidates(
        self, package_id: Optional[str] = None, status: Optional[str] = None
    ) -> List[Dict[str, Any]]:
        docs = self.db.collection("candidate_updates").stream()
        cands = [d.to_dict() for d in docs]
        if package_id:
            cands = [c for c in cands if c.get("package_id") == package_id]
        if status:
            cands = [c for c in cands if c.get("status") == status]
        cands.sort(key=lambda x: x.get("created_at", ""), reverse=True)
        return cands

    def save_candidate(self, candidate: Dict[str, Any]) -> str:
        cand_id = candidate.get("candidate_id") or f"cand-{str(uuid.uuid4())[:8]}"
        cand_copy = dict(candidate)
        cand_copy["candidate_id"] = cand_id
        if not cand_copy.get("created_at"):
            cand_copy["created_at"] = datetime.datetime.now(datetime.timezone.utc).isoformat()
        self.db.collection("candidate_updates").document(cand_id).set(cand_copy)
        return cand_id

    def update_candidate(self, candidate_id: str, updates: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        doc_ref = self.db.collection("candidate_updates").document(candidate_id)
        doc = doc_ref.get()
        if not doc.exists:
            return None
        data = doc.to_dict()
        doc_ref.set(updates, merge=True)
        data.update(updates)
        return data

    def delete_candidates(self, package_id: Optional[str] = None, exclude_status: Optional[str] = "MERGED") -> int:
        coll = self.db.collection("candidate_updates")
        deleted = 0
        batch = self.db.batch()
        for doc in coll.stream():
            data = doc.to_dict()
            if package_id is None or data.get("package_id") == package_id:
                if exclude_status and data.get("status") == exclude_status:
                    continue
                batch.delete(doc.reference)
                deleted += 1
                if deleted % 400 == 0:
                    batch.commit()
                    batch = self.db.batch()
        if deleted % 400 != 0:
            batch.commit()
        return deleted

    # Learned Rules
    def list_rules(self, package_id: Optional[str] = None) -> List[Dict[str, Any]]:
        docs = self.db.collection("learned_rules").stream()
        rules = [d.to_dict() for d in docs]
        if package_id:
            rules = [r for r in rules if r.get("package_id") == package_id or r.get("package_id") == "*"]
        rules.sort(key=lambda x: x.get("created_at", ""))
        return rules

    def add_rule(self, rule_data: Dict[str, Any]) -> str:
        rule_id = rule_data.get("rule_id") or f"rule-{str(uuid.uuid4())[:8]}"
        rule_copy = dict(rule_data)
        rule_copy["rule_id"] = rule_id
        if not rule_copy.get("created_at"):
            rule_copy["created_at"] = datetime.datetime.now(datetime.timezone.utc).isoformat()
        self.db.collection("learned_rules").document(rule_id).set(rule_copy)
        return rule_id

    def delete_rule(self, rule_id: str) -> bool:
        doc_ref = self.db.collection("learned_rules").document(rule_id)
        if doc_ref.get().exists:
            doc_ref.delete()
            return True
        return False

    # Audit Runs
    def record_audit_run(self, run_data: Dict[str, Any]) -> str:
        run_id = run_data.get("run_id") or f"run-{datetime.datetime.now().strftime('%Y%m%d_%H%M%S')}-{str(uuid.uuid4())[:4]}"
        run_copy = dict(run_data)
        run_copy["run_id"] = run_id
        if not run_copy.get("started_at"):
            run_copy["started_at"] = datetime.datetime.now(datetime.timezone.utc).isoformat()
        self.db.collection("audit_runs").document(run_id).set(run_copy)
        return run_id

    def list_audit_runs(self, limit: int = 10) -> List[Dict[str, Any]]:
        docs = self.db.collection("audit_runs").stream()
        runs = [d.to_dict() for d in docs]
        runs.sort(key=lambda x: x.get("started_at", ""), reverse=True)
        return runs[:limit]

    # Seeding & Migration
    def init_from_seed(self, force: bool = False):
        """Seeds canonical packages and rules into Firestore."""
        from tools.infra_updater.init_db import get_seed_data
        seed_data = get_seed_data()

        # If forcing full reseed (e.g. baseline reset), clear active candidate updates
        if force:
            deleted_cands = self.delete_candidates(package_id=None, exclude_status="MERGED")
            if deleted_cands > 0:
                print(f"[FirestoreDataStore] Cleared {deleted_cands} candidate update(s) during baseline reset.")

        batch = self.db.batch()
        count = 0

        for pid, pkg in seed_data.get("packages", {}).items():
            ref = self.db.collection("packages").document(pid)
            batch.set(ref, pkg, merge=not force)
            count += 1
            if count % 400 == 0:
                batch.commit()
                batch = self.db.batch()

        for rule in seed_data.get("learned_rules", []):
            rid = rule.get("rule_id")
            if rid:
                ref = self.db.collection("learned_rules").document(rid)
                batch.set(ref, rule, merge=not force)
                count += 1
                if count % 400 == 0:
                    batch.commit()
                    batch = self.db.batch()

        if count % 400 != 0:
            batch.commit()
        print(f"[SUCCESS] Seeded {count} documents into Firestore database '{self.database_id}'.")


# ==============================================================================
# Local JSON DataStore Implementation (Thread-Safe File)
# ==============================================================================

class JsonDataStore(BaseDataStore):
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

    # Packages
    def get_package(self, package_id: str) -> Optional[Dict[str, Any]]:
        with self._lock:
            data = self._read_data()
            pkg = data.get("packages", {}).get(package_id)
            return copy.deepcopy(pkg) if pkg else None

    def list_packages(self) -> List[Dict[str, Any]]:
        with self._lock:
            data = self._read_data()
            pkgs = list(data.get("packages", {}).values())
            pkgs.sort(key=lambda x: x.get("package_id", ""))
            return copy.deepcopy(pkgs)

    def update_package(self, package_id: str, updates: Dict[str, Any]) -> Optional[Dict[str, Any]]:
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

    # Candidates
    def get_candidate(self, candidate_id: str) -> Optional[Dict[str, Any]]:
        with self._lock:
            data = self._read_data()
            cand = data.get("candidate_updates", {}).get(candidate_id)
            return copy.deepcopy(cand) if cand else None

    def list_candidates(
        self, package_id: Optional[str] = None, status: Optional[str] = None
    ) -> List[Dict[str, Any]]:
        with self._lock:
            data = self._read_data()
            cands = list(data.get("candidate_updates", {}).values())
            if package_id:
                cands = [c for c in cands if c.get("package_id") == package_id]
            if status:
                cands = [c for c in cands if c.get("status") == status]
            cands.sort(key=lambda x: x.get("created_at", ""), reverse=True)
            return copy.deepcopy(cands)

    def save_candidate(self, candidate: Dict[str, Any]) -> str:
        cand_id = candidate.get("candidate_id") or f"cand-{str(uuid.uuid4())[:8]}"
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

    def delete_candidates(self, package_id: Optional[str] = None, exclude_status: Optional[str] = "MERGED") -> int:
        with self._lock:
            data = self._read_data()
            cands = data.setdefault("candidate_updates", {})
            to_delete = [
                cid for cid, c in cands.items()
                if (package_id is None or c.get("package_id") == package_id) and (exclude_status is None or c.get("status") != exclude_status)
            ]
            for cid in to_delete:
                del cands[cid]
            if to_delete:
                self._write_data(data)
            return len(to_delete)

    # Learned Rules
    def list_rules(self, package_id: Optional[str] = None) -> List[Dict[str, Any]]:
        with self._lock:
            data = self._read_data()
            rules = data.get("learned_rules", [])
            if package_id:
                rules = [r for r in rules if r.get("package_id") == package_id or r.get("package_id") == "*"]
            return copy.deepcopy(rules)

    def add_rule(self, rule_data: Dict[str, Any]) -> str:
        rule_id = rule_data.get("rule_id") or f"rule-{str(uuid.uuid4())[:8]}"
        rule_data["rule_id"] = rule_id
        if "created_at" not in rule_data:
            rule_data["created_at"] = datetime.datetime.now(datetime.timezone.utc).isoformat()

        with self._lock:
            data = self._read_data()
            rules = data.setdefault("learned_rules", [])
            rules.append(copy.deepcopy(rule_data))
            self._write_data(data)
            return rule_id

    def delete_rule(self, rule_id: str) -> bool:
        with self._lock:
            data = self._read_data()
            rules = data.get("learned_rules", [])
            new_rules = [r for r in rules if r.get("rule_id") != rule_id]
            if len(new_rules) < len(rules):
                data["learned_rules"] = new_rules
                self._write_data(data)
                return True
            return False

    # Audit Runs
    def record_audit_run(self, run_data: Dict[str, Any]) -> str:
        run_id = run_data.get("run_id") or f"run-{datetime.datetime.now().strftime('%Y%m%d_%H%M%S')}-{str(uuid.uuid4())[:4]}"
        run_data["run_id"] = run_id
        if "started_at" not in run_data:
            run_data["started_at"] = datetime.datetime.now(datetime.timezone.utc).isoformat()

        with self._lock:
            data = self._read_data()
            runs = data.setdefault("audit_runs", [])
            runs.insert(0, copy.deepcopy(run_data))
            self._write_data(data)
            return run_id

    def list_audit_runs(self, limit: int = 10) -> List[Dict[str, Any]]:
        with self._lock:
            data = self._read_data()
            runs = data.get("audit_runs", [])
            return copy.deepcopy(runs[:limit])

    # Seeding
    def init_from_seed(self, force: bool = False):
        if os.path.exists(self.json_path) and not force:
            return

        from tools.infra_updater.init_db import get_seed_data
        seed_data = get_seed_data()
        self._write_data(seed_data)
        print(f"[SUCCESS] Initialized JSON state store at {self.json_path}")


# Alias DataStore to JsonDataStore for backward compatibility with type annotations
DataStore = JsonDataStore


# ==============================================================================
# Factory & Singleton Management
# ==============================================================================

_GLOBAL_STORE: Optional[BaseDataStore] = None


def get_datastore(force_provider: Optional[str] = None) -> BaseDataStore:
    """
    Returns the process-wide DataStore singleton.
    Dynamically resolves between Cloud Firestore and local JSON based on config.
    """
    global _GLOBAL_STORE
    cfg = get_config()
    provider = force_provider or cfg.database.provider.lower()

    if _GLOBAL_STORE is None:
        if provider in ("firestore", "firebase"):
            _GLOBAL_STORE = FirestoreDataStore(
                project_id=cfg.database.project_id,
                database_id=cfg.database.database_id
            )
        else:
            _GLOBAL_STORE = JsonDataStore()
    return _GLOBAL_STORE


def reset_datastore_singleton():
    """Resets the singleton instance (useful during tests or provider switches)."""
    global _GLOBAL_STORE
    _GLOBAL_STORE = None
