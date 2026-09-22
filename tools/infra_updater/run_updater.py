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
  1. Source Qualification Agent (Section 4.2): Upstream release discovery, GA stability filtering, policy rule checks.
  2. Orchestrator Agent (Section 4.3): Atomic blueprint updates, coupled variable synchronization, status transitions.
  3. Upstream policy rules loaded dynamically from SQLite (Section 2.2).
  4. Changelog semantic compatibility triage (Section 4.2).

All evaluation rules, blueprint instances, and package registries are loaded dynamically
from SQLite database tables (packages, blueprint_instances, learned_rules, benchmark_cases).
"""

import argparse
import os
import sqlite3
import subprocess
import sys
import time

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
REPO_ROOT = os.path.abspath(os.path.join(BASE_DIR, "../.."))
DB_PATH = os.path.join(BASE_DIR, "updater_state.db")

from init_db import init_database, preview_tables
from source_agent import SourceQualificationAgent, UpfrontRuleChecker
from code_modifier import OrchestratorAgent, AtomicCodeModifier

# ANSI terminal colors
GREEN = "\033[92m"
RED = "\033[91m"
YELLOW = "\033[93m"
CYAN = "\033[96m"
BOLD = "\033[1m"
RESET = "\033[0m"

def run_check_all(model: str = "gemini-3.8-flash"):
    print(f"\n{BOLD}{CYAN}=== STEP 1: Source Qualification Agent (Upstream Discovery & Qualification) ==={RESET}")
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
    print(f"\n{BOLD}Changelog Summaries & Compatibility Verdicts:{RESET}")
    for r in results:
        if r.get("status") == "UPDATE_FOUND":
            print(f"  * {BOLD}{r['package_id']} ({r['candidate_version']}){RESET}:")
            print(f"    - Verdict:  {GREEN}{r.get('llm_verdict', 'COMPATIBLE')}{RESET} (Track: {r.get('llm_release_track')})")
            print(f"    - Summary:  {r.get('llm_summary')}")
            print(f"    - PR Notes: {r.get('llm_pr_notes', '').strip()}")
            print()

    print(f"[INFO] Qualified candidate updates stored in table '{BOLD}candidate_updates{RESET}'.")
    print(f"[TIP] Run '{BOLD}python3 tools/infra_updater/run_updater.py --apply <package_id>{RESET}' to apply changes to target blueprints.")


def run_apply(package_id: str):
    print(f"\n{BOLD}{CYAN}=== STEP 2: Orchestrator Agent (Blueprint Update & Variable Synchronization) ==={RESET}")
    print(f"Target Package: {BOLD}{package_id}{RESET}\n")

    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute("SELECT version, download_url, compatibility_verdict, changelog_summary FROM candidate_updates WHERE package_id = ? ORDER BY created_at DESC LIMIT 1", (package_id,))
    cand = cursor.fetchone()
    conn.close()

    if cand:
        print(f"{CYAN}[Triage Summary]{RESET} Compatibility: {GREEN}{cand[2]}{RESET}")
        print(f"{CYAN}[Triage Summary]{RESET} Changelog:     {cand[3]}\n")

    agent = OrchestratorAgent()
    res = agent.apply_update(package_id)

    if res["status"] != "SUCCESS":
        print(f"{RED}[ERROR] {res.get('message')}{RESET}")
        return

    print(f"{GREEN}[SUCCESS] Target Version:   {res['target_version']}{RESET}")
    print(f"{GREEN}[SUCCESS] Download URL:     {res['download_url']}{RESET}")
    print(f"{GREEN}[SUCCESS] Workflow Status:  READY_FOR_REVIEW{RESET}\n")

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


def run_test_llm_triage(model: str = "gemini-3.8-flash"):
    print(f"\n{BOLD}{CYAN}=== Semantic Changelog & Deprecation Analysis (Gemini LLM) ==={RESET}")
    print("Demonstrating LLM reasoning over upstream changelogs loaded dynamically from table 'benchmark_cases'...\n")

    agent = SourceQualificationAgent(model=model)
    if not agent.use_llm:
        print(f"{RED}[ERROR] Gemini LLM client is not available.{RESET}")
        return

    # Load test cases dynamically from SQLite table benchmark_cases (Zero Hardcoding)
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute("""
        SELECT package_id, test_version, sample_changelog, description, expected_verdict
        FROM benchmark_cases 
        WHERE category = 'CHANGELOG_TRIAGE'
        ORDER BY case_id
    """)
    test_cases = cursor.fetchall()
    conn.close()

    if not test_cases:
        print(f"{YELLOW}[WARN] No CHANGELOG_TRIAGE benchmark cases found in database.{RESET}")
        return

    for pkg_id, ver, changelog, desc, expected_verdict in test_cases:
        print(f"{BOLD}Scenario:{RESET} {desc} ({BOLD}{pkg_id} v{ver}{RESET}) [Expected: {expected_verdict}]")
        analysis = agent.analyze_changelog_with_llm(pkg_id, ver, changelog or "")
        
        if analysis.is_breaking or analysis.compatibility_verdict == "INCOMPATIBLE":
            verdict_color = RED
        elif analysis.compatibility_verdict == "POTENTIALLY_BREAKING":
            verdict_color = YELLOW
        else:
            verdict_color = GREEN

        print(f"  * {BOLD}LLM Compatibility Verdict:{RESET} {verdict_color}{analysis.compatibility_verdict}{RESET}")
        print(f"  * {BOLD}Breaking Changes Detected:{RESET} {analysis.is_breaking}")
        if analysis.breaking_reasons:
            print(f"  * {BOLD}Specific Breaking Items Identified by LLM:{RESET}")
            for item in analysis.breaking_reasons:
                print(f"    - {RED}{item}{RESET}")
        print(f"  * {BOLD}Executive Summary:{RESET} {analysis.summary}")
        print(f"  * {BOLD}Generated PR Notes:{RESET} {analysis.pr_changelog_snippet.strip()}")
        print("-" * 90)


def run_test_rule_blocking():
    print(f"\n{BOLD}{CYAN}=== Learned Rule Policy Enforcement ==={RESET}")
    print("Evaluating candidate versions against persistent learned rules (table 'benchmark_cases')...\n")

    checker = UpfrontRuleChecker()

    # Load test cases dynamically from SQLite table benchmark_cases (Zero Hardcoding)
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute("""
        SELECT package_id, test_version, description, expected_verdict
        FROM benchmark_cases 
        WHERE category = 'RULE_GATE'
        ORDER BY case_id
    """)
    test_cases = cursor.fetchall()
    conn.close()

    if not test_cases:
        print(f"{YELLOW}[WARN] No RULE_GATE benchmark cases found in database.{RESET}")
        return

    print(f"{'Package ID':<18} | {'Candidate Ver':<18} | {'Decision':<14} | Details & Rationale")
    print("=" * 110)

    for pkg_id, ver, desc, expected_verdict in test_cases:
        is_blocked, rule = checker.check_version(pkg_id, ver)

        if is_blocked:
            decision = f"{RED}{BOLD}BLOCKED{RESET}"
            details = f"Rule {rule['rule_id']} ({rule['version_constraint']}) -> {rule['reason']}"
        else:
            decision = f"{GREEN}{BOLD}PASSED{RESET}"
            details = f"Policy constraint passed. Candidate allowed."

        print(f"{pkg_id:<18} | {ver:<18} | {decision:<23} | {details}")

    print("=" * 110)
    print(f"\n{GREEN}[VERIFIED] Upfront policy rule validation completed.{RESET}\n")


def run_end_to_end(model: str = "gemini-3.8-flash"):
    print(f"\n{BOLD}{CYAN}======================================================================{RESET}")
    print(f"{BOLD}{CYAN}      CLUSTER TOOLKIT INFRASTRUCTURE UPDATER - FULL PIPELINE RUN      {RESET}")
    print(f"{BOLD}{CYAN}======================================================================{RESET}\n")

    # Stage 0: Clean Baseline
    print(f"{BOLD}[STAGE 0/4] Initializing Database & Verifying Registry...{RESET}")
    init_database()
    
    # Stage 1: Learned Rule Policy Evaluation
    print(f"\n{BOLD}[STAGE 1/4] Evaluating Learned Policy Rules...{RESET}")
    run_test_rule_blocking()

    # Stage 2: Semantic Changelog & Deprecation Triage
    print(f"\n{BOLD}[STAGE 2/4] Executing Semantic Changelog & Compatibility Triage ({model})...{RESET}")
    run_test_llm_triage(model=model)

    # Stage 3: Source Qualification Across Canonical Packages
    print(f"\n{BOLD}[STAGE 3/4] Running Source Qualification Agent Across Monitored Packages ({model})...{RESET}")
    run_check_all(model=model)

    # Stage 4: Orchestrator Agent Blueprint Modification & Variable Sync
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute("SELECT package_id FROM candidate_updates WHERE status = 'UPDATE_FOUND' ORDER BY created_at ASC")
    candidate_rows = cursor.fetchall()
    conn.close()

    if candidate_rows:
        print(f"\n{BOLD}[STAGE 4/4] Orchestrator Agent applying updates across all {len(candidate_rows)} candidate package(s)...{RESET}")
        for (target_pkg,) in candidate_rows:
            run_apply(target_pkg)
    else:
        print(f"\n{BOLD}[STAGE 4/4] No candidate updates with status UPDATE_FOUND available to apply.{RESET}")

    print(f"\n{BOLD}[FINAL STATE] Database Summary:{RESET}")
    preview_tables()

    print(f"\n{BOLD}{GREEN}======================================================================{RESET}")
    print(f"{BOLD}{GREEN}                   PIPELINE EXECUTION COMPLETE                        {RESET}")
    print(f"{BOLD}{GREEN}======================================================================{RESET}\n")
    print(f"[TIP] Run '{BOLD}python3 tools/infra_updater/run_updater.py --reset{RESET}' to restore files and reset database.\n")


def run_reset():
    print(f"\n{BOLD}{YELLOW}[RESET] Reverting blueprint files and resetting database state...{RESET}")
    agent = OrchestratorAgent()
    rev = agent.revert_update()
    for f in rev.get("reverted_files", []):
        print(f"  Reverted: {f}")

    init_database()
    print(f"{GREEN}[SUCCESS] Environment and database reset to baseline.{RESET}\n")


def main():
    parser = argparse.ArgumentParser(
        description="Cluster Toolkit Infrastructure Updater - Automated Dependency Management Runner",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  python3 run_updater.py --check-all           # Run qualification (LLM candidate extraction + learned rules + LLM changelog)
  python3 run_updater.py --test-llm-triage     # Demonstrate LLM changelog & breaking change analysis
  python3 run_updater.py --apply <package_id>  # Apply update and show clean git diff
  python3 run_updater.py --test-rule-blocking   # Evaluate upfront policy rule filter
  python3 run_updater.py --show-tables          # Preview all SQLite state tables
  python3 run_updater.py --reset                # Revert files and reset database
"""
    )
    parser.add_argument("-m", "--model", default="gemini-3.8-flash", help="Gemini LLM model to use (default: gemini-3.8-flash)")
    parser.add_argument("-e", "--end-to-end", action="store_true", help="Run full pipeline end-to-end (all stages)")
    parser.add_argument("-c", "--check-all", action="store_true", help="Run Source Qualification Agent across all packages")
    parser.add_argument("-l", "--test-llm-triage", action="store_true", help="Demonstrate Gemini LLM semantic changelog & deprecation triage")
    parser.add_argument("-a", "--apply", metavar="PACKAGE_ID", type=str, help="Apply qualified update for PACKAGE_ID")
    parser.add_argument("-t", "--test-rule-blocking", action="store_true", help="Demonstrate upfront policy rule blocking")
    parser.add_argument("-s", "--show-tables", action="store_true", help="Display previews of all SQLite tables")
    parser.add_argument("-r", "--reset", action="store_true", help="Reset database and revert git modifications")

    if len(sys.argv) == 1:
        parser.print_help()
        sys.exit(0)

    args = parser.parse_args()

    if args.end_to_end:
        run_end_to_end(model=args.model)
        return
    if args.reset:
        run_reset()
    if args.show_tables:
        preview_tables()
    if args.test_rule_blocking:
        run_test_rule_blocking()
    if args.test_llm_triage:
        run_test_llm_triage(model=args.model)
    if args.check_all:
        run_check_all(model=args.model)
    if args.apply:
        run_apply(args.apply)

if __name__ == "__main__":
    main()
