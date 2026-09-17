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
Interactive Web UI Server for Cluster Toolkit Infrastructure Updater POC.
Serves modern, minimal dashboard and provides REST API for end-to-end triggers.
"""

import argparse
from http.server import ThreadingHTTPServer, SimpleHTTPRequestHandler
import io
import json
import os
import re
import sqlite3
import subprocess
import sys
import threading
import time

POC_DIR = os.path.dirname(os.path.abspath(__file__))
REPO_ROOT = os.path.abspath(os.path.join(POC_DIR, "../../.."))
UI_DIR = os.path.join(POC_DIR, "ui")
DB_PATH = os.path.join(POC_DIR, "poc_state.db")

# Global execution state buffer
class LogStreamBuffer:
    def __init__(self, max_lines=2000):
        self.lock = threading.Lock()
        self.lines = []
        self.max_lines = max_lines
        self.is_running = False
        self.current_action = "IDLE"
        self.last_status = "IDLE"

    def write_line(self, line):
        with self.lock:
            self.lines.append(line)
            if len(self.lines) > self.max_lines:
                self.lines.pop(0)

    def clear(self):
        with self.lock:
            self.lines = []

    def get_logs(self):
        with self.lock:
            return "".join(self.lines)

GLOBAL_BUFFER = LogStreamBuffer()

def execute_cli_action(cmd_args, action_name):
    GLOBAL_BUFFER.is_running = True
    GLOBAL_BUFFER.current_action = action_name
    GLOBAL_BUFFER.write_line(f"\n>>> [{time.strftime('%H:%M:%S')}] STARTING ACTION: {action_name} <<<\n")

    env = os.environ.copy()
    env["PYTHONUNBUFFERED"] = "1"
    python_exec = sys.executable

    try:
        proc = subprocess.Popen(
            [python_exec, "-u", os.path.join(POC_DIR, "run_poc.py")] + cmd_args,
            cwd=REPO_ROOT,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
            env=env
        )

        for line in iter(proc.stdout.readline, ''):
            clean_line = re.sub(r'\033\[[0-9;]*m', '', line)
            GLOBAL_BUFFER.write_line(clean_line)

        proc.stdout.close()
        return_code = proc.wait()

        if return_code == 0:
            GLOBAL_BUFFER.last_status = "SUCCESS"
            GLOBAL_BUFFER.write_line(f"\n>>> [{time.strftime('%H:%M:%S')}] COMPLETED SUCCESSFULLY <<<\n")
        else:
            GLOBAL_BUFFER.last_status = "FAILED"
            GLOBAL_BUFFER.write_line(f"\n>>> [{time.strftime('%H:%M:%S')}] FAILED WITH CODE {return_code} <<<\n")

    except Exception as e:
        GLOBAL_BUFFER.last_status = "ERROR"
        GLOBAL_BUFFER.write_line(f"\n>>> [ERROR] Exception executing {action_name}: {e} <<<\n")
    finally:
        GLOBAL_BUFFER.is_running = False
        GLOBAL_BUFFER.current_action = "IDLE"


class POCDashboardHandler(SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=UI_DIR, **kwargs)

    def do_GET(self):
        if self.path == "/api/state":
            self.handle_get_state()
        elif self.path.startswith("/api/logs"):
            self.handle_get_logs()
        elif self.path.startswith("/api/diff"):
            self.handle_get_diff()
        else:
            # Static files
            super().do_GET()

    def do_POST(self):
        if self.path == "/api/action":
            self.handle_post_action()
        else:
            self.send_error(404, "Endpoint not found")

    def handle_get_state(self):
        if not os.path.exists(DB_PATH):
            from init_db import init_database
            init_database()

        conn = sqlite3.connect(DB_PATH)
        cursor = conn.cursor()

        # 1. Packages
        cursor.execute("PRAGMA table_info(packages)")
        cols = [r[1] for r in cursor.fetchall()]
        has_upstream = "upstream_version" in cols
        has_summary = "qualification_summary" in cols
        has_type = "upstream_type" in cols
        has_pattern = "version_pattern" in cols

        query = f"""
            SELECT package_id, name, current_version, 
                   {'upstream_version' if has_upstream else "'-'"} as upstream_ver,
                   source_url, 
                   {'upstream_type' if has_type else "'generic'"} as up_type,
                   {'version_pattern' if has_pattern else "NULL"} as v_pattern,
                   status, 
                   {'qualification_summary' if has_summary else "'Registered baseline.'"} as qual_summary,
                   updated_at 
            FROM packages ORDER BY package_id
        """
        cursor.execute(query)
        packages = [
            {
                "package_id": r[0], "name": r[1], "current_version": r[2],
                "upstream_version": r[3] or "-", "source_url": r[4], 
                "upstream_type": r[5] or "generic",
                "version_pattern": r[6] or "",
                "status": r[7], "qualification_summary": r[8] or "Baseline registered. Run qualification to evaluate.",
                "updated_at": r[9]
            }
            for r in cursor.fetchall()
        ]

        # 2. Blueprint Instances
        cursor.execute("SELECT instance_id, package_id, blueprint_path, variable_name, coupled_vars FROM blueprint_instances ORDER BY instance_id")
        instances = [
            {
                "instance_id": r[0], "package_id": r[1], "blueprint_path": r[2],
                "variable_name": r[3], "coupled_vars": json.loads(r[4])
            }
            for r in cursor.fetchall()
        ]

        # 3. Learned Rules
        cursor.execute("SELECT rule_id, package_id, rule_type, version_constraint, action, reason, created_at FROM learned_rules ORDER BY rule_id")
        rules = [
            {
                "rule_id": r[0], "package_id": r[1], "rule_type": r[2],
                "version_constraint": r[3], "action": r[4], "reason": r[5], "created_at": r[6]
            }
            for r in cursor.fetchall()
        ]

        # 4. Candidate Updates
        cursor.execute("SELECT candidate_id, package_id, version, download_url, status, compatibility_verdict, changelog_summary, created_at FROM candidate_updates ORDER BY created_at DESC")
        candidates = [
            {
                "candidate_id": r[0], "package_id": r[1], "version": r[2],
                "download_url": r[3], "status": r[4],
                "compatibility_verdict": r[5] or "UNKNOWN",
                "changelog_summary": r[6] or "No changelog summary generated.",
                "created_at": r[7]
            }
            for r in cursor.fetchall()
        ]

        # 5. Benchmark Cases
        cursor.execute("SELECT case_id, category, package_id, test_version, expected_verdict, description FROM benchmark_cases ORDER BY category, case_id")
        benchmarks = [
            {
                "case_id": r[0], "category": r[1], "package_id": r[2],
                "test_version": r[3], "expected_verdict": r[4], "description": r[5]
            }
            for r in cursor.fetchall()
        ]

        conn.close()

        # Git diff stats
        diff_proc = subprocess.run(
            ["git", "diff", "--stat", "examples/"],
            cwd=REPO_ROOT, stdout=subprocess.PIPE, text=True, check=False
        )
        git_diff_stat = diff_proc.stdout.strip()

        payload = {
            "packages": packages,
            "instances": instances,
            "rules": rules,
            "candidates": candidates,
            "benchmarks": benchmarks,
            "git_diff_stat": git_diff_stat,
            "has_modifications": bool(git_diff_stat),
            "is_running": GLOBAL_BUFFER.is_running,
            "current_action": GLOBAL_BUFFER.current_action,
            "last_status": GLOBAL_BUFFER.last_status,
            "stats": {
                "total_packages": len(packages),
                "total_instances": len(instances),
                "total_rules": len(rules),
                "total_benchmarks": len(benchmarks),
                "pending_updates": len([c for c in candidates if c["status"] in ("UPDATE_FOUND", "QUALIFIED")]),
                "ready_updates": len([c for c in candidates if c["status"] == "READY_FOR_REVIEW"]),
                "qualified_candidates": len([c for c in candidates if c["status"] in ("UPDATE_FOUND", "QUALIFIED")]),
                "applied_candidates": len([c for c in candidates if c["status"] in ("READY_FOR_REVIEW", "APPLIED")])
            }
        }

        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(json.dumps(payload).encode("utf-8"))

    def handle_get_logs(self):
        payload = {
            "logs": GLOBAL_BUFFER.get_logs(),
            "is_running": GLOBAL_BUFFER.is_running,
            "current_action": GLOBAL_BUFFER.current_action,
            "last_status": GLOBAL_BUFFER.last_status
        }
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(json.dumps(payload).encode("utf-8"))

    def handle_get_diff(self):
        diff_proc = subprocess.run(
            ["git", "diff", "examples/"],
            cwd=REPO_ROOT, stdout=subprocess.PIPE, text=True, check=False
        )
        payload = {"diff": diff_proc.stdout}
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(json.dumps(payload).encode("utf-8"))

    def handle_post_action(self):
        content_length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(content_length).decode("utf-8")
        data = json.loads(body) if body else {}

        action = data.get("action")
        pkg_id = data.get("package_id")

        if GLOBAL_BUFFER.is_running:
            self.send_response(409)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps({"error": "Another action is currently running"}).encode("utf-8"))
            return

        cmd_args = []
        action_name = action

        if action == "end_to_end":
            cmd_args = ["--end-to-end"]
            action_name = "Full Pipeline Run"
        elif action == "check_all":
            cmd_args = ["--check-all"]
            action_name = "Source Qualification Agent"
        elif action == "test_llm_triage":
            cmd_args = ["--test-llm-triage"]
            action_name = "Changelog Compatibility Triage"
        elif action == "test_rule_blocking":
            cmd_args = ["--test-rule-blocking"]
            action_name = "Learned Rule Policy Evaluation"
        elif action == "apply":
            if not pkg_id:
                self.send_error(400, "package_id required for apply")
                return
            cmd_args = ["--apply", pkg_id]
            action_name = f"Orchestrator Agent Update ({pkg_id})"
        elif action == "reset":
            cmd_args = ["--reset"]
            action_name = "Reset Environment & Database"
        else:
            self.send_error(400, f"Unknown action: {action}")
            return

        GLOBAL_BUFFER.clear()
        t = threading.Thread(target=execute_cli_action, args=(cmd_args, action_name), daemon=True)
        t.start()

        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(json.dumps({"status": "STARTED", "action": action_name}).encode("utf-8"))


def run_server(port=8080):
    server = ThreadingHTTPServer(("0.0.0.0", port), POCDashboardHandler)
    print(f"\n======================================================================")
    print(f"  CLUSTER TOOLKIT UPDATER - WEB DASHBOARD SERVER")
    print(f"======================================================================")
    print(f"  Local Access:   http://localhost:{port}")
    print(f"  Network Access: http://127.0.0.1:{port}")
    print(f"======================================================================\n")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nShutting down server...")
        server.server_close()

if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=8080, help="Port to bind server (default: 8080)")
    args = parser.parse_args()
    run_server(args.port)
