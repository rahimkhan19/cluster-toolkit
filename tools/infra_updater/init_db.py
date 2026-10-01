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
Seeds / refreshes the state store from packages.yaml and the target repository's blueprints.

Packages come from packages.yaml; blueprint instances and each package's current version are
discovered from the workspace checkout of the base branch (registry.discover).
  python3 init_db.py                          reset to the baseline (clears non-merged candidates)
  python3 init_db.py --refresh                refresh registry + blueprints, keep runtime state
  python3 init_db.py --migrate-to-firestore   copy updater_state.json into Firestore
"""

import datetime
import json
import os
import sys
from typing import Any, Dict, Optional

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
if BASE_DIR not in sys.path:
    sys.path.insert(0, BASE_DIR)

from config import get_config  # noqa: E402
from registry import discover, load_registry, package_current_version  # noqa: E402
from statuses import PackageStatus  # noqa: E402

JSON_PATH = os.path.join(BASE_DIR, "updater_state.json")


def get_seed_data(workspace: Optional[str] = None) -> Dict[str, Any]:
    """Baseline documents for every registered package, with discovered blueprint instances."""
    workspace = workspace or get_config().get_workspace_path()
    registry = load_registry()
    if os.path.isdir(os.path.join(workspace, ".git")):
        found = discover(workspace, registry)
    else:
        print(f"[WARN] No workspace checkout at {workspace}; packages are seeded without blueprints.")
        found = {}
    now = datetime.datetime.now(datetime.timezone.utc).isoformat()
    packages = {}
    for pid, p in registry.packages.items():
        blueprints = found.get(pid, [])
        packages[pid] = {
            "package_id": pid,
            "name": p.name,
            "source_url": p.source_url,
            "upstream_type": p.upstream_type,
            "source_options": p.source_options,
            "blueprints": blueprints,
            "disabled_blueprints": [],
            "current_version": package_current_version(blueprints, []) or "-",
            "upstream_version": "-",
            "status": PackageStatus.REGISTERED,
            "qualification_summary": "Monitored baseline.",
            "snooze_until": None,
            "updated_at": now,
        }
    return {"packages": packages, "candidate_updates": {}, "learned_rules": [], "audit_runs": []}


def init_database(reset: bool = False, sync_workspace: bool = True):
    """
    Seeds the configured datastore (Firestore or JSON).
    reset=False: add new packages and refresh registry fields and blueprints; keep status, snooze/block,
                 blueprint deselections and candidates.
    reset=True:  restore the baseline and clear all non-merged candidates.
    sync_workspace: first reset the workspace to the latest base branch so discovery reads it.
    """
    from datastore import get_datastore
    if sync_workspace:
        from repo_manager import RepoManager
        RepoManager(get_config()).ensure_workspace(force_clean=True)
    get_datastore().init_from_seed(force=reset)


def preview_tables():
    from datastore import get_datastore
    store = get_datastore()
    print(f"\n{'Package ID':<18} | {'Current':<26} | {'Upstream':<24} | {'Source Type':<15} | {'Status':<16} | Blueprints")
    print("-" * 130)
    for p in store.list_packages():
        bps = p.get("blueprints", [])
        selected = sum(1 for b in bps if b.get("enabled"))
        print(f"{p['package_id']:<18} | {p.get('current_version') or '-':<26} | {p.get('upstream_version') or '-':<24} | "
              f"{p.get('upstream_type', '-'):<15} | {p.get('status', '-'):<16} | {selected}/{len(bps)}")
    print(f"\nCandidates: {len(store.list_candidates())} | Learned rules: {len(store.list_rules())}")


def migrate_to_firestore(json_path: str = JSON_PATH):
    """Copies all entities from the local JSON state file into Cloud Firestore."""
    from google.cloud import firestore

    cfg = get_config()
    if not os.path.exists(json_path):
        print(f"[ERROR] Source JSON file {json_path} does not exist.")
        return
    with open(json_path, "r", encoding="utf-8") as f:
        data = json.load(f)
    db = firestore.Client(project=cfg.database.project_id, database=cfg.database.database_id)
    batch, count = db.batch(), 0
    collections = [("packages", data.get("packages", {}).items()),
                   ("candidate_updates", data.get("candidate_updates", {}).items()),
                   ("learned_rules", ((r.get("rule_id"), r) for r in data.get("learned_rules", []))),
                   ("audit_runs", ((r.get("run_id"), r) for r in data.get("audit_runs", [])))]
    for name, docs in collections:
        for doc_id, doc in docs:
            if doc_id:
                batch.set(db.collection(name).document(doc_id), doc)
                count += 1
    batch.commit()
    print(f"[SUCCESS] Migrated {count} documents to Cloud Firestore database '{cfg.database.database_id}'.")


if __name__ == "__main__":
    if "--migrate-to-firestore" in sys.argv:
        migrate_to_firestore()
    else:
        init_database(reset="--refresh" not in sys.argv)
    preview_tables()
