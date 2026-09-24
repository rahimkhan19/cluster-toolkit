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
Interactive Web UI Server for Cluster Toolkit Infrastructure Updater.
Serves modern, minimal dashboard and provides REST API for end-to-end triggers.
"""

import argparse
from http.server import ThreadingHTTPServer, SimpleHTTPRequestHandler
import io
import json
import os
import re
import subprocess
import sys
import threading
import time

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
if BASE_DIR not in sys.path:
    sys.path.insert(0, BASE_DIR)
REPO_ROOT = os.path.abspath(os.path.join(BASE_DIR, "../.."))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

try:
    from tools.infra_updater.config import get_config
    from tools.infra_updater.datastore import get_datastore
    from tools.infra_updater.repo_manager import RepoManager
except ModuleNotFoundError:
    from config import get_config
    from datastore import get_datastore
    from repo_manager import RepoManager

CONFIG = get_config()
REPO_MANAGER = RepoManager(CONFIG)
UI_DIR = os.path.join(BASE_DIR, "ui")

# Global execution state buffer
class LogStreamBuffer:
    def __init__(self, max_lines=1000):
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

_GIT_DIFF_CACHE = {"stat": "", "timestamp": 0.0}

def get_cached_git_diff_stat(force: bool = False) -> str:
    now = time.time()
    ttl = 5.0
    if force or (now - _GIT_DIFF_CACHE["timestamp"] > ttl):
        try:
            _GIT_DIFF_CACHE["stat"] = REPO_MANAGER.get_diff_stat()
        except Exception:
            _GIT_DIFF_CACHE["stat"] = ""
        _GIT_DIFF_CACHE["timestamp"] = now
    return _GIT_DIFF_CACHE["stat"]

def invalidate_git_diff_cache():
    _GIT_DIFF_CACHE["timestamp"] = 0.0

def execute_cli_action(cmd_args, action_name):
    GLOBAL_BUFFER.is_running = True
    GLOBAL_BUFFER.current_action = action_name
    GLOBAL_BUFFER.write_line(f"\n>>> [{time.strftime('%H:%M:%S')}] STARTING ACTION: {action_name} <<<\n")

    env = os.environ.copy()
    env["PYTHONUNBUFFERED"] = "1"
    python_exec = sys.executable

    try:
        proc = subprocess.Popen(
            [python_exec, "-u", os.path.join(BASE_DIR, "run_updater.py")] + cmd_args,
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
            invalidate_git_diff_cache()
            GLOBAL_BUFFER.last_status = "SUCCESS"
            GLOBAL_BUFFER.write_line(f"\n>>> [{time.strftime('%H:%M:%S')}] COMPLETED SUCCESSFULLY <<<\n")
        else:
            invalidate_git_diff_cache()
            GLOBAL_BUFFER.last_status = "FAILED"
            GLOBAL_BUFFER.write_line(f"\n>>> [{time.strftime('%H:%M:%S')}] FAILED WITH CODE {return_code} <<<\n")

    except Exception as e:
        GLOBAL_BUFFER.last_status = "ERROR"
        GLOBAL_BUFFER.write_line(f"\n>>> [ERROR] Exception executing {action_name}: {e} <<<\n")
    finally:
        invalidate_git_diff_cache()
        GLOBAL_BUFFER.is_running = False
        GLOBAL_BUFFER.current_action = "IDLE"

_LAST_PR_SYNC_TIME = 0.0
PR_SYNC_COOLDOWN = float(CONFIG.server.pr_sync_interval_seconds)  # Seconds between background GitHub PR status checks (default 60s)


class DashboardHandler(SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=UI_DIR, **kwargs)

    def end_headers(self):
        self.send_header("Cache-Control", "no-store, no-cache, must-revalidate, max-age=0")
        self.send_header("Pragma", "no-cache")
        self.send_header("Expires", "0")
        super().end_headers()

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
        try:
            store = get_datastore()

            # Automatically sync GitHub PR statuses if cooldown has elapsed
            global _LAST_PR_SYNC_TIME
            now = time.time()
            if (now - _LAST_PR_SYNC_TIME) > PR_SYNC_COOLDOWN:
                _LAST_PR_SYNC_TIME = now
                try:
                    REPO_MANAGER.sync_open_pr_statuses(store)
                except Exception as ex:
                    print(f"[Server] Background PR sync warning: {ex}", flush=True)

            packages = store.list_packages()
            instances = store.list_all_blueprints()
            rules = store.list_rules()
            candidates = store.list_candidates()
            audit_runs = store.list_audit_runs(limit=20)

            # Git diff stats (cached with 5s TTL to prevent continuous subprocess execution)
            git_diff_stat = get_cached_git_diff_stat()

            payload = {
                "packages": packages,
                "instances": instances,
                "rules": rules,
                "candidates": candidates,
                "audit_runs": audit_runs,
                "git_diff_stat": git_diff_stat,
                "has_modifications": bool(git_diff_stat),
                "is_running": GLOBAL_BUFFER.is_running,
                "current_action": GLOBAL_BUFFER.current_action,
                "last_status": GLOBAL_BUFFER.last_status,
                "config": {
                    "repo_url": CONFIG.repository.url,
                    "owner": CONFIG.repository.owner,
                    "repo_name": CONFIG.repository.name,
                    "base_branch": CONFIG.repository.base_branch,
                    "llm_model": CONFIG.llm.model,
                    "token_present": bool(CONFIG.get_github_token()),
                    "workspace_dir": REPO_MANAGER.workspace_dir
                },
                "stats": {
                    "total_packages": len(packages),
                    "total_instances": len(instances),
                    "total_rules": len(rules),
                    "pending_updates": len([c for c in candidates if c.get("status") in ("UPDATE_FOUND", "QUALIFIED")]),
                    "ready_updates": len([c for c in candidates if c.get("status") == "READY_FOR_REVIEW"]),
                    "qualified_candidates": len([c for c in candidates if c.get("status") in ("UPDATE_FOUND", "QUALIFIED")]),
                    "applied_candidates": len([c for c in candidates if c.get("status") in ("READY_FOR_REVIEW", "APPLIED")])
                }
            }

            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Access-Control-Allow-Origin", "*")
            self.end_headers()
            self.wfile.write(json.dumps(payload).encode("utf-8"))
        except Exception as e:
            import traceback
            traceback.print_exc()
            self.send_response(500)
            self.send_header("Content-Type", "application/json")
            self.send_header("Access-Control-Allow-Origin", "*")
            self.end_headers()
            self.wfile.write(json.dumps({"error": str(e)}).encode("utf-8"))

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
        diff_text = REPO_MANAGER.get_diff()
        payload = {"diff": diff_text}
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

        if action == "sync_repo":
            cmd_args = ["--sync-repo"]
            action_name = "Sync Target Repository"
        elif action == "sync_prs":
            cmd_args = ["--sync-prs"]
            action_name = "Sync GitHub Pull Requests"
        elif action == "end_to_end":
            cmd_args = ["--end-to-end"]
            action_name = "Full Pipeline Run"
        elif action == "check_all":
            cmd_args = ["--check-all"]
            action_name = "Source Qualification Agent"
        elif action == "test_rule_blocking":
            cmd_args = ["--test-rule-blocking"]
            action_name = "Learned Rule Policy Evaluation"
        elif action == "apply":
            if not pkg_id:
                self.send_error(400, "package_id required for apply")
                return
            cmd_args = ["--apply", pkg_id]
            action_name = f"Orchestrator Agent Update ({pkg_id})"
        elif action == "create_pr":
            if not pkg_id:
                self.send_error(400, "package_id required for create_pr")
                return
            cmd_args = ["--create-pr", pkg_id]
            action_name = f"Create GitHub PR ({pkg_id})"
        elif action == "reset":
            cmd_args = ["--reset"]
            action_name = "Reset Environment & State"
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


def run_server(port=None):
    if port is None:
        port = CONFIG.server.port
    host = "0.0.0.0"

    # Ensure target workspace develop branch is in sync with origin on startup
    try:
        REPO_MANAGER.ensure_workspace(force_clean=True)
    except Exception as e:
        print(f"[Server] [WARN] Initial workspace sync error: {e}", flush=True)

    server = ThreadingHTTPServer((host, port), DashboardHandler)
    print(f"\n======================================================================")
    print(f"  CLUSTER TOOLKIT UPDATER - WEB DASHBOARD SERVER")
    print(f"======================================================================")
    print(f"  Target Repo:    {CONFIG.repository.url} ({CONFIG.repository.branch})")
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
    parser.add_argument("--port", type=int, default=CONFIG.server.port, help=f"Port to bind server (default: {CONFIG.server.port})")
    args = parser.parse_args()
    run_server(args.port)
