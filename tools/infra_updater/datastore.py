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
1. Cloud Firestore (Native Mode on GCP) via ADC or service account credentials.
2. Local thread-safe JSON datastore (updater_state.json) for offline/local development.

Both backends implement BaseDataStore, providing CRUD for:
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

from config import get_config
from registry import package_current_version
from statuses import ACTIVE_CANDIDATE_STATUSES, CandidateStatus

BASE_DIR = os.path.dirname(os.path.abspath(__file__))


def get_default_json_path() -> str:
    return os.path.join(BASE_DIR, "updater_state.json")


def _now() -> str:
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


# Package fields owned by packages.yaml + blueprint discovery. On a non-reset seed only these are
# refreshed on existing packages; runtime state (status, snooze/block, candidates) is preserved.
SEED_REGISTRY_FIELDS = ("name", "source_url", "upstream_type", "source_options", "blueprints")


def registry_updates(existing: Dict[str, Any], seed_pkg: Dict[str, Any]) -> Dict[str, Any]:
    """
    Fields to write on an existing package: the registry fields, the blueprint deselections carried
    over to the newly discovered instances (matched by id, or by blueprint path for legacy ids), and
    the current version recomputed from the selected blueprints.
    """
    updates = {f: copy.deepcopy(seed_pkg[f]) for f in SEED_REGISTRY_FIELDS if f in seed_pkg}
    new_ids = {b["instance_id"] for b in updates.get("blueprints", [])}
    legacy_paths = {b.get("instance_id"): b.get("blueprint_path") for b in existing.get("blueprints", [])}
    disabled = sorted({d if d in new_ids else legacy_paths.get(d) for d in existing.get("disabled_blueprints") or []}
                      & new_ids)
    updates["disabled_blueprints"] = disabled
    updates["current_version"] = package_current_version(updates.get("blueprints", []), disabled) \
        or existing.get("current_version") or "-"
    return updates


def merge_seed_package(existing: Optional[Dict[str, Any]], seed_pkg: Dict[str, Any]) -> Dict[str, Any]:
    """Returns the package document to store: the seed for new packages, else existing + registry updates."""
    if not existing:
        return copy.deepcopy(seed_pkg)
    return {**copy.deepcopy(existing), **registry_updates(existing, seed_pkg)}


def _exclude_set(exclude_status: Optional[Any]) -> set:
    if not exclude_status:
        return set()
    if isinstance(exclude_status, (list, tuple, set, frozenset)):
        return set(exclude_status)
    return {exclude_status}


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

    @staticmethod
    def _normalize_package_blueprints(pkg: Optional[Dict[str, Any]]) -> None:
        if not pkg:
            return
        # disabled_blueprints is the single source of truth; "enabled" is derived on read.
        disabled = set(pkg.get("disabled_blueprints") or [])
        for bp in pkg.get("blueprints", []):
            bp["enabled"] = bp.get("instance_id") not in disabled

    # Blueprints
    def get_blueprints_for_package(self, package_id: str) -> List[Dict[str, Any]]:
        pkg = self.get_package(package_id)
        return copy.deepcopy(pkg.get("blueprints", [])) if pkg else []

    def list_all_blueprints(self) -> List[Dict[str, Any]]:
        all_bps = []
        for pkg in self.list_packages():
            pid = pkg.get("package_id")
            for bp in pkg.get("blueprints", []):
                bp_copy = copy.deepcopy(bp)
                bp_copy["package_id"] = pid
                bp_copy["package_name"] = pkg.get("name", pid)
                all_bps.append(bp_copy)
        return all_bps

    def update_blueprint_selection(
        self,
        package_id: str,
        action: str = "toggle_blueprint",
        instance_id: Optional[str] = None,
        enabled: Optional[bool] = None,
    ) -> Optional[Dict[str, Any]]:
        pkg = self.get_package(package_id)
        if not pkg:
            return None

        blueprints = copy.deepcopy(pkg.get("blueprints", []))
        disabled = set(pkg.get("disabled_blueprints") or [])
        if action == "select_all_blueprints":
            disabled.clear()
        elif action == "deselect_all_blueprints":
            disabled.update(bp["instance_id"] for bp in blueprints if bp.get("instance_id"))
        elif action == "toggle_blueprint" and instance_id:
            select = bool(enabled) if enabled is not None else instance_id in disabled
            (disabled.discard if select else disabled.add)(instance_id)

        current_version = package_current_version(blueprints, sorted(disabled)) or pkg.get("current_version")
        self.update_package(package_id, {"disabled_blueprints": sorted(disabled), "current_version": current_version})
        for bp in blueprints:
            bp["enabled"] = bp.get("instance_id") not in disabled

        selected_count = sum(1 for bp in blueprints if bp["enabled"])
        total_count = len(blueprints)
        return {
            "status": "SUCCESS",
            "package_id": package_id,
            "blueprints": blueprints,
            "disabled_blueprints": sorted(disabled),
            "current_version": current_version,
            "selected_count": selected_count,
            "total_count": total_count,
            "message": f"Updated blueprint selection for {package_id} ({selected_count}/{total_count} selected).",
        }

    # Candidates
    @staticmethod
    def _prepare_candidate(candidate: Dict[str, Any]) -> Dict[str, Any]:
        """Copy of the candidate with a generated id and created_at timestamp when missing."""
        cand = copy.deepcopy(candidate)
        cand["candidate_id"] = cand.get("candidate_id") or f"cand-{uuid.uuid4().hex[:8]}"
        cand.setdefault("created_at", _now())
        return cand

    @staticmethod
    def _prepare_audit_run(run_data: Dict[str, Any]) -> Dict[str, Any]:
        run = copy.deepcopy(run_data)
        run["run_id"] = run.get("run_id") or f"run-{datetime.datetime.now().strftime('%Y%m%d_%H%M%S')}-{uuid.uuid4().hex[:4]}"
        run.setdefault("started_at", _now())
        return run

    @staticmethod
    def _filter_candidates(
        cands: List[Dict[str, Any]], package_id: Optional[str], status: Optional[str]
    ) -> List[Dict[str, Any]]:
        """Filters by package/status and sorts newest-first by created_at."""
        out = [c for c in cands
               if (not package_id or c.get("package_id") == package_id) and (not status or c.get("status") == status)]
        out.sort(key=lambda x: x.get("created_at", ""), reverse=True)
        return out

    @abc.abstractmethod
    def get_candidate(self, candidate_id: str) -> Optional[Dict[str, Any]]:
        pass

    @abc.abstractmethod
    def list_candidates(
        self, package_id: Optional[str] = None, status: Optional[str] = None
    ) -> List[Dict[str, Any]]:
        """Returns candidates sorted newest-first by created_at."""
        pass

    def get_active_candidate(self, package_id: str) -> Optional[Dict[str, Any]]:
        """Newest actionable candidate, else the newest non-merged one."""
        cands = self.list_candidates(package_id=package_id)
        return (next((c for c in cands if c.get("status") in ACTIVE_CANDIDATE_STATUSES), None)
                or next((c for c in cands if c.get("status") != CandidateStatus.MERGED), None))

    @abc.abstractmethod
    def save_candidate(self, candidate: Dict[str, Any]) -> str:
        pass

    @abc.abstractmethod
    def update_candidate(self, candidate_id: str, updates: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        pass

    @abc.abstractmethod
    def delete_candidate(self, candidate_id: str) -> bool:
        pass

    @abc.abstractmethod
    def delete_candidates(self, package_id: Optional[str] = None, exclude_status: Optional[Any] = CandidateStatus.MERGED) -> int:
        pass

    # Learned Rules
    @abc.abstractmethod
    def list_rules(self, package_id: Optional[str] = None) -> List[Dict[str, Any]]:
        pass

    # Audit Runs
    @abc.abstractmethod
    def record_audit_run(self, run_data: Dict[str, Any]) -> str:
        pass

    # Seeding
    @abc.abstractmethod
    def init_from_seed(self, force: bool = False):
        """
        force=True: reset to the seed baseline (clears non-merged candidates, overwrites packages).
        force=False: add new seed packages and refresh registry fields; preserve runtime state.
        """
        pass


# ==============================================================================
# Cloud Firestore DataStore Implementation
# ==============================================================================

class FirestoreDataStore(BaseDataStore):
    """
    Cloud Firestore DataStore implementation.
    Operates on Google Cloud Firestore in Native Mode using Application Default Credentials (ADC).
    """

    def __init__(self, project_id: Optional[str] = None, database_id: Optional[str] = None):
        cfg = get_config()
        self.project_id = project_id or cfg.database.project_id
        self.database_id = database_id or cfg.database.database_id
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
            if next(self.db.collection("packages").limit(1).stream(), None) is None:
                print(f"[FirestoreDataStore] Initializing empty Firestore database '{self.database_id}' from seed data...")
                self.init_from_seed()
        except Exception as ex:
            print(f"[FirestoreDataStore] [WARN] Initialization check failed: {ex}")

    # Packages
    def get_package(self, package_id: str) -> Optional[Dict[str, Any]]:
        doc = self.db.collection("packages").document(package_id).get()
        if not doc.exists:
            return None
        pkg = doc.to_dict()
        self._normalize_package_blueprints(pkg)
        return pkg

    def list_packages(self) -> List[Dict[str, Any]]:
        pkgs = [d.to_dict() for d in self.db.collection("packages").stream()]
        for pkg in pkgs:
            self._normalize_package_blueprints(pkg)
        pkgs.sort(key=lambda x: x.get("package_id", ""))
        return pkgs

    def update_package(self, package_id: str, updates: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        doc_ref = self.db.collection("packages").document(package_id)
        doc = doc_ref.get()
        if not doc.exists:
            return None
        data = doc.to_dict()
        updates_with_ts = {**updates, "updated_at": _now()}
        doc_ref.set(updates_with_ts, merge=True)
        data.update(updates_with_ts)
        self._normalize_package_blueprints(data)
        return data

    # Candidates
    def get_candidate(self, candidate_id: str) -> Optional[Dict[str, Any]]:
        doc = self.db.collection("candidate_updates").document(candidate_id).get()
        return doc.to_dict() if doc.exists else None

    def list_candidates(
        self, package_id: Optional[str] = None, status: Optional[str] = None
    ) -> List[Dict[str, Any]]:
        from google.cloud.firestore_v1.base_query import FieldFilter
        query = self.db.collection("candidate_updates")
        # One server-side equality filter (no composite index needed); the rest is filtered locally.
        if package_id:
            query = query.where(filter=FieldFilter("package_id", "==", package_id))
        elif status:
            query = query.where(filter=FieldFilter("status", "==", str(status)))
        return self._filter_candidates([d.to_dict() for d in query.stream()], package_id, status)

    def save_candidate(self, candidate: Dict[str, Any]) -> str:
        cand = self._prepare_candidate(candidate)
        self.db.collection("candidate_updates").document(cand["candidate_id"]).set(cand)
        return cand["candidate_id"]

    def update_candidate(self, candidate_id: str, updates: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        doc_ref = self.db.collection("candidate_updates").document(candidate_id)
        doc = doc_ref.get()
        if not doc.exists:
            return None
        data = doc.to_dict()
        doc_ref.set(updates, merge=True)
        data.update(updates)
        return data

    def delete_candidate(self, candidate_id: str) -> bool:
        doc_ref = self.db.collection("candidate_updates").document(candidate_id)
        if doc_ref.get().exists:
            doc_ref.delete()
            return True
        return False

    def delete_candidates(self, package_id: Optional[str] = None, exclude_status: Optional[Any] = CandidateStatus.MERGED) -> int:
        exclude = _exclude_set(exclude_status)
        deleted = 0
        batch = self.db.batch()
        for doc in self.db.collection("candidate_updates").stream():
            data = doc.to_dict()
            if package_id is not None and data.get("package_id") != package_id:
                continue
            if data.get("status") in exclude:
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
        rules = [d.to_dict() for d in self.db.collection("learned_rules").stream()]
        if package_id:
            rules = [r for r in rules if r.get("package_id") in (package_id, "*")]
        rules.sort(key=lambda x: x.get("created_at", ""))
        return rules

    # Audit Runs
    def record_audit_run(self, run_data: Dict[str, Any]) -> str:
        run = self._prepare_audit_run(run_data)
        self.db.collection("audit_runs").document(run["run_id"]).set(run)
        return run["run_id"]

    # Seeding
    def init_from_seed(self, force: bool = False):
        from init_db import get_seed_data
        seed_data = get_seed_data()

        if force:
            deleted_cands = self.delete_candidates()
            if deleted_cands > 0:
                print(f"[FirestoreDataStore] Cleared {deleted_cands} candidate update(s) during baseline reset.")

        existing = {} if force else {d.id: d.to_dict() for d in self.db.collection("packages").stream()}
        batch = self.db.batch()
        count = 0

        def _write(ref, doc, update=False):
            nonlocal batch, count
            (batch.update if update else batch.set)(ref, doc)
            count += 1
            if count % 400 == 0:
                batch.commit()
                batch = self.db.batch()

        for pid, pkg in seed_data.get("packages", {}).items():
            ref = self.db.collection("packages").document(pid)
            if pid in existing:  # only registry fields, so concurrent runtime updates are not overwritten
                _write(ref, registry_updates(existing[pid], pkg), update=True)
            else:
                _write(ref, pkg)

        if count % 400 != 0:
            batch.commit()
        mode = "reset" if force else "refreshed (runtime state preserved)"
        print(f"[SUCCESS] Seeded {count} documents into Firestore database '{self.database_id}' ({mode}).")


# ==============================================================================
# Local JSON DataStore Implementation (Thread-Safe File)
# ==============================================================================

class JsonDataStore(BaseDataStore):
    """Thread-safe, atomic JSON data store for dependency registry and lifecycle state."""

    def __init__(self, json_path: Optional[str] = None):
        self.json_path = json_path or os.environ.get("UPDATER_STATE_JSON", get_default_json_path())
        self._lock = threading.Lock()
        if not os.path.exists(self.json_path):
            self.init_from_seed()

    @staticmethod
    def _empty() -> Dict[str, Any]:
        return {"packages": {}, "candidate_updates": {}, "learned_rules": [], "audit_runs": []}

    def _read_data(self) -> Dict[str, Any]:
        """Reads and returns state dictionary from JSON file."""
        if not os.path.exists(self.json_path):
            return self._empty()
        with open(self.json_path, "r", encoding="utf-8") as f:
            return json.load(f)

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
            pkg = self._read_data().get("packages", {}).get(package_id)
        if not pkg:
            return None
        self._normalize_package_blueprints(pkg)
        return pkg

    def list_packages(self) -> List[Dict[str, Any]]:
        with self._lock:
            pkgs = list(self._read_data().get("packages", {}).values())
        for pkg in pkgs:
            self._normalize_package_blueprints(pkg)
        pkgs.sort(key=lambda x: x.get("package_id", ""))
        return pkgs

    def update_package(self, package_id: str, updates: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        with self._lock:
            data = self._read_data()
            pkg = data.setdefault("packages", {}).get(package_id)
            if pkg is None:
                return None
            pkg.update(copy.deepcopy(updates))
            pkg["updated_at"] = _now()
            self._write_data(data)
        self._normalize_package_blueprints(pkg)
        return pkg

    # Candidates
    def get_candidate(self, candidate_id: str) -> Optional[Dict[str, Any]]:
        with self._lock:
            return self._read_data().get("candidate_updates", {}).get(candidate_id)

    def list_candidates(
        self, package_id: Optional[str] = None, status: Optional[str] = None
    ) -> List[Dict[str, Any]]:
        with self._lock:
            cands = list(self._read_data().get("candidate_updates", {}).values())
        return self._filter_candidates(cands, package_id, status)

    def save_candidate(self, candidate: Dict[str, Any]) -> str:
        cand = self._prepare_candidate(candidate)
        with self._lock:
            data = self._read_data()
            data.setdefault("candidate_updates", {})[cand["candidate_id"]] = cand
            self._write_data(data)
        return cand["candidate_id"]

    def update_candidate(self, candidate_id: str, updates: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        with self._lock:
            data = self._read_data()
            cand = data.setdefault("candidate_updates", {}).get(candidate_id)
            if cand is None:
                return None
            cand.update(copy.deepcopy(updates))
            self._write_data(data)
        return cand

    def delete_candidate(self, candidate_id: str) -> bool:
        with self._lock:
            data = self._read_data()
            cands = data.setdefault("candidate_updates", {})
            if candidate_id not in cands:
                return False
            del cands[candidate_id]
            self._write_data(data)
            return True

    def delete_candidates(self, package_id: Optional[str] = None, exclude_status: Optional[Any] = CandidateStatus.MERGED) -> int:
        exclude = _exclude_set(exclude_status)
        with self._lock:
            data = self._read_data()
            cands = data.setdefault("candidate_updates", {})
            to_delete = [
                cid for cid, c in cands.items()
                if (package_id is None or c.get("package_id") == package_id) and c.get("status") not in exclude
            ]
            for cid in to_delete:
                del cands[cid]
            if to_delete:
                self._write_data(data)
            return len(to_delete)

    # Learned Rules
    def list_rules(self, package_id: Optional[str] = None) -> List[Dict[str, Any]]:
        with self._lock:
            rules = self._read_data().get("learned_rules", [])
        if package_id:
            rules = [r for r in rules if r.get("package_id") in (package_id, "*")]
        return rules

    # Audit Runs
    def record_audit_run(self, run_data: Dict[str, Any]) -> str:
        run = self._prepare_audit_run(run_data)
        with self._lock:
            data = self._read_data()
            data.setdefault("audit_runs", []).insert(0, run)
            self._write_data(data)
        return run["run_id"]

    # Seeding
    def init_from_seed(self, force: bool = False):
        from init_db import get_seed_data
        seed_data = get_seed_data()

        with self._lock:
            if force or not os.path.exists(self.json_path):
                old = self._read_data().get("candidate_updates", {})
                data = seed_data
                # Baseline reset keeps merged history, like the Firestore backend.
                data["candidate_updates"] = {k: v for k, v in old.items() if v.get("status") == CandidateStatus.MERGED}
            else:
                data = self._read_data()
                packages = data.setdefault("packages", {})
                for pid, pkg in seed_data.get("packages", {}).items():
                    packages[pid] = merge_seed_package(packages.get(pid), pkg)
            self._write_data(data)
        print(f"[SUCCESS] Seeded JSON state store at {self.json_path}")


# ==============================================================================
# Factory & Singleton Management
# ==============================================================================

_GLOBAL_STORE: Optional[BaseDataStore] = None


def get_datastore() -> BaseDataStore:
    """
    Returns the process-wide DataStore singleton.
    Resolves between Cloud Firestore and local JSON based on config (database.provider).
    """
    global _GLOBAL_STORE
    if _GLOBAL_STORE is None:
        cfg = get_config()
        if cfg.database.provider == "firestore":
            _GLOBAL_STORE = FirestoreDataStore(cfg.database.project_id, cfg.database.database_id)
        else:
            _GLOBAL_STORE = JsonDataStore()
    return _GLOBAL_STORE
