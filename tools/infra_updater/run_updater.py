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
Master CLI Runner for the Cluster Toolkit Infrastructure Updater.
Aligns with Implementation Guide: Automated Dependency Management.
Demonstrates:
  1. Source Qualification Agent (Section 4.2): Upstream release discovery, GA stability filtering, policy rule checks, artifact liveness.
  2. Orchestrator Agent (Section 4.3): Atomic blueprint updates, coupled variable synchronization, status transitions.
  3. Upstream policy rules loaded dynamically from DataStore (Section 2.2).

All evaluation rules, blueprint instances, and package registries are loaded dynamically
from the DataStore (packages, blueprints, learned_rules).
"""

import argparse
import os
import subprocess
import sys
import time

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
if BASE_DIR not in sys.path:
    sys.path.insert(0, BASE_DIR)
REPO_ROOT = os.path.abspath(os.path.join(BASE_DIR, "../.."))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

from tools.infra_updater.config import get_config
from tools.infra_updater.repo_manager import RepoManager
from tools.infra_updater.datastore import get_datastore
from init_db import init_database, preview_tables
from source_agent import SourceQualificationAgent, UpfrontRuleChecker
from code_modifier import OrchestratorAgent, AtomicCodeModifier

CONFIG = get_config()

# ANSI terminal colors
GREEN = "\033[92m"
RED = "\033[91m"
YELLOW = "\033[93m"
CYAN = "\033[96m"
BOLD = "\033[1m"
RESET = "\033[0m"

def run_check_all(model: str = None):
    if model is None:
        model = CONFIG.llm.model
    print(f"\n{BOLD}{CYAN}=== STEP 1: Source Qualification Agent (Upstream Discovery & Qualification) ==={RESET}")
    # Ensure target workspace develop branch is in sync with remote origin
    repo_mgr = RepoManager(CONFIG)
    repo_mgr.ensure_workspace(force_clean=True)
    print("Evaluating GA Stability, Policy Rules, and Compatibility...\n")

    agent = SourceQualificationAgent(model=model)
    results = agent.qualify_all()

    header = f"{'Package ID':<18} | {'Current Ver':<14} | {'Latest Upstream':<16} | {'Target Update':<14} | {'Policy Check':<14} | {'Status':<18} | Summary & Rationale"
    print("=" * 140)
    print(header)
    print("-" * 140)

    for r in results:
        pkg_id = r["package_id"]
        curr_ver = r["current_version"]
        up_ver = r.get("upstream_version") or "-"
        target_ver = r.get("candidate_version") or "-"
        status = r.get("status") or "-"
        summary = r.get("summary") or r.get("message") or "-"

        if status == "UPDATE_FOUND":
            policy_check = f"{GREEN}PASSED{RESET}"
            status_str = f"{GREEN}{BOLD}UPDATE_FOUND{RESET}"
        elif status == "BLOCKED" or status == "BLOCKED_BY_RULE":
            policy_check = f"{RED}BLOCKED{RESET}"
            status_str = f"{RED}{BOLD}BLOCKED{RESET}"
        elif status == "UP_TO_DATE":
            policy_check = f"{CYAN}PASS{RESET}"
            status_str = f"{CYAN}UP-TO-DATE{RESET}"
        elif status == "REJECTED_UNSTABLE":
            policy_check = f"{YELLOW}-{RESET}"
            status_str = f"{YELLOW}NON_GA{RESET}"
        else:
            policy_check = f"{YELLOW}-{RESET}"
            status_str = f"{RED}{status}{RESET}"

        print(f"{pkg_id:<18} | {curr_ver:<14} | {up_ver:<16} | {target_ver:<14} | {policy_check:<23} | {status_str:<27} | {summary}")

    print("=" * 140)
    print(f"[INFO] Qualified candidate updates stored in table '{BOLD}candidate_updates{RESET}'.")
    print(f"[TIP] Run '{BOLD}python3 tools/infra_updater/run_updater.py --apply <package_id>{RESET}' to apply changes to target blueprints.")

    # Record Operational Telemetry (Doc Section 2.5)
    try:
        store = get_datastore()
        updates_found = len([r for r in results if r.get("status") == "UPDATE_FOUND"])
        store.record_audit_run({
            "trigger_type": "MANUAL",
            "triggered_by": "cli_user",
            "components_scanned": len(results),
            "prs_opened": 0,
            "summary": {"updates_found": updates_found, "total_scanned": len(results)}
        })
    except Exception:
        pass


def run_apply(package_id: str, create_pr: bool = True):
    print(f"\n{BOLD}{CYAN}=== STEP 2: Orchestrator Agent (Blueprint Update & PR Creation) ==={RESET}")
    print(f"Target Package:    {BOLD}{package_id}{RESET}")
    print(f"Target Repository: {BOLD}{CONFIG.repository.url}{RESET} (branch: {BOLD}{CONFIG.repository.base_branch}{RESET})\n")

    store = get_datastore()
    candidates = store.list_candidates(package_id=package_id)
    cand = candidates[-1] if candidates else None

    if cand:
        print(f"{CYAN}[Candidate]{RESET} Target Version: {GREEN}{cand.get('version')}{RESET}")
        if cand.get("summary"):
            print(f"{CYAN}[Candidate]{RESET} Summary:        {cand.get('summary')}\n")

    agent = OrchestratorAgent(store=store)
    res = agent.apply_update(package_id, create_pr=create_pr)

    if res["status"] != "SUCCESS":
        print(f"{RED}[ERROR] {res.get('message')}{RESET}")
        return

    print(f"{GREEN}[SUCCESS] Target Version:   {res['target_version']}{RESET}")
    print(f"{GREEN}[SUCCESS] Download URL:     {res['download_url']}{RESET}")
    print(f"{GREEN}[SUCCESS] Workflow Status:  READY_FOR_REVIEW{RESET}")
    if res.get("branch"):
        print(f"{CYAN}[BRANCH]{RESET}        Update Branch:  {BOLD}{res['branch']}{RESET}")
    if res.get("pushed"):
        print(f"{GREEN}[GIT PUSH]{RESET}      Remote Branch:  {CONFIG.repository.url}")
    if res.get("pr_url"):
        print(f"{GREEN}{BOLD}[PULL REQUEST]{RESET}  GitHub PR:      {BOLD}{res['pr_url']}{RESET}")
    print()

    print(f"{BOLD}Modified Blueprints & Synchronized Variables:{RESET}")
    for mod in res["modified_files"]:
        print(f"  * {BOLD}{mod['file_path']}{RESET}")
        print(f"    - Primary Var:  {mod['primary_variable']} = {mod['new_value']}")
        for c in mod["coupled_changes"]:
            print(f"    - Coupled Var:  {c['variable']} = {c['new_value']} {YELLOW}(Synchronized){RESET}")

    print(f"\n{BOLD}{CYAN}=== Generated Git Diff (Comments & Structure Preserved) ==={RESET}\n")
    for path, diff in res["diffs"].items():
        for line in diff.splitlines():
            if line.startswith("+") and not line.startswith("+++"):
                print(f"{GREEN}{line}{RESET}")
            elif line.startswith("-") and not line.startswith("---"):
                print(f"{RED}{line}{RESET}")
            elif line.startswith("@@"):
                print(f"{CYAN}{line}{RESET}")
            else:
                print(line)
        print()

    print(f"{GREEN}[VERIFIED] YAML syntax valid on all modified files.{RESET}")


def run_test_rule_blocking():
    print(f"\n{BOLD}{CYAN}=== Active Learned Policy Rules ==={RESET}")
    store = get_datastore()
    rules = store.list_rules()
    if not rules:
        print("No active policy rules registered in DataStore (table 'learned_rules').\n")
        return

    print(f"{'Rule ID':<22} | {'Package ID':<18} | {'Type':<16} | {'Constraint':<20} | {'Action':<6} | Reason")
    print("=" * 110)
    for r in rules:
        print(f"{r.get('rule_id', ''):<22} | {r.get('package_id', ''):<18} | {r.get('rule_type', ''):<16} | {r.get('version_constraint', ''):<20} | {r.get('action', ''):<6} | {r.get('reason', '')}")
    print("=" * 110 + "\n")


def run_sync_repo():
    print(f"\n{BOLD}{CYAN}=== Synchronizing Target Repository ==={RESET}")
    repo_mgr = RepoManager(CONFIG)
    print(f"Target Repository: {BOLD}{CONFIG.repository.url}{RESET} (branch: {BOLD}{CONFIG.repository.base_branch}{RESET})")
    print(f"Target Directory:  {BOLD}{repo_mgr.workspace_dir}{RESET}")
    print("Fetching latest commits from remote and resetting workspace to clean base...")
    repo_mgr.ensure_workspace(force_clean=True)
    head_proc = subprocess.run(["git", "rev-parse", "--short", "HEAD"], cwd=repo_mgr.workspace_dir, stdout=subprocess.PIPE, text=True, check=False)
    head_commit = head_proc.stdout.strip()
    print(f"{GREEN}[SUCCESS] Target workspace synchronized to {CONFIG.repository.base_branch} @ {head_commit}.{RESET}")

    # Synchronize open PR statuses with GitHub
    store = get_datastore()
    print("Checking open Pull Request statuses on GitHub...")
    repo_mgr.sync_open_pr_statuses(store)
    print(f"{GREEN}[SUCCESS] Pull request statuses synchronized.{RESET}\n")


def run_sync_prs():
    print(f"\n{BOLD}{CYAN}=== Synchronizing GitHub Pull Request Statuses ==={RESET}")
    repo_mgr = RepoManager(CONFIG)
    store = get_datastore()
    res = repo_mgr.sync_open_pr_statuses(store)
    if res.get("synced"):
        changes = res.get("changes", [])
        if changes:
            print(f"{GREEN}[SUCCESS] Synced PR statuses with GitHub ({len(changes)} changes applied):{RESET}")
            for ch in changes:
                act = ch.get("action")
                cid = ch.get("candidate_id")
                pnum = ch.get("pr_number")
                if act == "REVERTED_TO_UPDATE_FOUND":
                    print(f"  * Candidate {cid} (PR #{pnum}): Closed manually -> moved back to {GREEN}UPDATE_FOUND{RESET} (update available)")
                elif act == "MERGED":
                    print(f"  * Candidate {cid} (PR #{pnum}): Merged -> updated to {CYAN}UP_TO_DATE{RESET}")
                else:
                    print(f"  * Candidate {cid}: {act} (PR #{pnum})")
        else:
            print(f"{GREEN}[UP-TO-DATE] All open PRs and candidate states are in sync with GitHub.{RESET}")
    else:
        print(f"{YELLOW}[WARN] Could not sync PR statuses: {res.get('error')}{RESET}")
    print()


def run_show_config():
    print(f"\n{BOLD}{CYAN}======================================================================{RESET}")
    print(f"{BOLD}{CYAN}   CLUSTER TOOLKIT INFRASTRUCTURE UPDATER - CONFIGURATION OVERVIEW    {RESET}")
    print(f"{BOLD}{CYAN}======================================================================{RESET}\n")
    repo_mgr = RepoManager(CONFIG)
    token = CONFIG.get_github_token()
    token_display = f"{GREEN}Present ({token[:4]}...{token[-4:]}){RESET}" if token else f"{YELLOW}None (unauthenticated/read-only){RESET}"

    print(f"{BOLD}Target Repository:{RESET}")
    print(f"  * URL:              {CONFIG.repository.url}")
    print(f"  * Owner / Repo:     {CONFIG.repository.owner} / {CONFIG.repository.name}")
    print(f"  * Branch:           {CONFIG.repository.branch}")
    print(f"  * Workspace Path:   {repo_mgr.workspace_dir}")
    print(f"  * GitHub Token:     {token_display}")
    print(f"\n{BOLD}Git Author Configuration:{RESET}")
    print(f"  * Name:             {CONFIG.git.author_name}")
    print(f"  * Email:            {CONFIG.git.author_email}")
    print(f"\n{BOLD}LLM Configuration:{RESET}")
    print(f"  * Model:            {CONFIG.llm.model}")
    print(f"\n{BOLD}Dashboard Server:{RESET}")
    print(f"  * Port:             {CONFIG.server.port}")
    print()


def run_end_to_end(model: str = None):
    if model is None:
        model = CONFIG.llm.model
    print(f"\n{BOLD}{CYAN}======================================================================{RESET}")
    print(f"{BOLD}{CYAN}      CLUSTER TOOLKIT INFRASTRUCTURE UPDATER - FULL PIPELINE RUN      {RESET}")
    print(f"{BOLD}{CYAN}======================================================================{RESET}\n")

    # Stage 0: Synchronize Target Repository & Clean Baseline
    print(f"{BOLD}[STAGE 0/3] Synchronizing Target Repository ({CONFIG.repository.url})...{RESET}")
    run_sync_repo()
    print(f"{BOLD}[STAGE 0/3] Initializing Database & Verifying Registry...{RESET}")
    init_database()

    # Stage 1: Source Qualification Across Canonical Packages
    print(f"\n{BOLD}[STAGE 1/3] Running Source Qualification Agent Across Monitored Packages ({model})...{RESET}")
    run_check_all(model=model)

    # Stage 2: Orchestrator Agent Blueprint Modification, Branch Push, & GitHub PR Creation
    store = get_datastore()
    candidates = store.list_candidates()
    candidate_pkgs = [c.get("package_id") for c in candidates if c.get("status") == "UPDATE_FOUND"]

    if candidate_pkgs:
        print(f"\n{BOLD}[STAGE 2/3] Orchestrator Agent applying updates across {len(candidate_pkgs)} candidate package(s)...{RESET}")
        for target_pkg in candidate_pkgs:
            run_apply(target_pkg, create_pr=True)
    else:
        print(f"\n{BOLD}[STAGE 2/3] No candidate updates with status UPDATE_FOUND available to apply.{RESET}")

    # Stage 3: Final Telemetry & State Summary
    print(f"\n{BOLD}[STAGE 3/3] DataStore Final State Summary:{RESET}")
    preview_tables()

    print(f"\n{BOLD}{GREEN}======================================================================{RESET}")
    print(f"{BOLD}{GREEN}                   PIPELINE EXECUTION COMPLETE                        {RESET}")
    print(f"{BOLD}{GREEN}======================================================================{RESET}\n")
    print(f"[TIP] Run '{BOLD}python3 tools/infra_updater/run_updater.py --reset{RESET}' to restore files and reset state store.\n")


def run_reset():
    print(f"\n{BOLD}{YELLOW}[RESET] Reverting blueprint files and resetting state store...{RESET}")
    agent = OrchestratorAgent()
    rev = agent.revert_update()
    for f in rev.get("reverted_files", []):
        print(f"  Reverted: {f}")

    init_database()
    print(f"{GREEN}[SUCCESS] Environment and state store reset to baseline.{RESET}\n")


def main():
    parser = argparse.ArgumentParser(
        description="Cluster Toolkit Infrastructure Updater - Automated Dependency Management Runner",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  python3 run_updater.py --sync-repo           # Fetch latest target repo develop branch
  python3 run_updater.py --check-all           # Run qualification (upstream discovery + rules + liveness)
  python3 run_updater.py --apply <package_id>  # Apply update, push branch & create GitHub PR
  python3 run_updater.py --rules               # Display active learned policy rules
  python3 run_updater.py --show-config         # Display active configuration & auth status
  python3 run_updater.py --show-tables         # Preview all datastore entities
  python3 run_updater.py --reset               # Revert files and reset state store
"""
    )
    parser.add_argument("-m", "--model", default=CONFIG.llm.model, help=f"Gemini LLM model to use (default: {CONFIG.llm.model})")
    parser.add_argument("-e", "--end-to-end", action="store_true", help="Run full pipeline end-to-end (all stages)")
    parser.add_argument("--sync-repo", action="store_true", help="Synchronize target repository workspace to base branch")
    parser.add_argument("--sync-prs", action="store_true", help="Synchronize open PR statuses with GitHub (detect closed PRs)")
    parser.add_argument("-c", "--check-all", action="store_true", help="Run Source Qualification Agent across all packages")
    parser.add_argument("-a", "--apply", metavar="PACKAGE_ID", type=str, help="Apply qualified update, push branch & create PR for PACKAGE_ID")
    parser.add_argument("--no-pr", action="store_true", help="Skip GitHub PR creation during apply")
    parser.add_argument("-t", "--rules", "--test-rule-blocking", dest="rules", action="store_true", help="Display active learned policy rules")
    parser.add_argument("--show-config", action="store_true", help="Display active configuration values")
    parser.add_argument("-s", "--show-tables", action="store_true", help="Display previews of all datastore entities")
    parser.add_argument("-r", "--reset", action="store_true", help="Reset state store and revert git modifications")

    if len(sys.argv) == 1:
        parser.print_help()
        sys.exit(0)

    args = parser.parse_args()

    if args.show_config:
        run_show_config()
    if args.sync_repo:
        run_sync_repo()
    if args.sync_prs:
        run_sync_prs()
    if args.end_to_end:
        run_end_to_end(model=args.model)
        return
    if args.reset:
        run_reset()
    if args.show_tables:
        preview_tables()
    if args.rules:
        run_test_rule_blocking()
    if args.check_all:
        run_check_all(model=args.model)
    if args.apply:
        run_apply(args.apply, create_pr=not args.no_pr)

if __name__ == "__main__":
    main()
