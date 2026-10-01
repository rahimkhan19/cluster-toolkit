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
Serves the dashboard, exposes a small REST API, runs CLI actions as subprocesses,
and runs one background poller that syncs PR statuses and Cloud Build test results.
"""

import argparse
import collections
from http.server import ThreadingHTTPServer, SimpleHTTPRequestHandler
import json
import os
import re
import socket
import subprocess
import sys
import threading
import time

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
if BASE_DIR not in sys.path:
    sys.path.insert(0, BASE_DIR)
from config import get_config
from datastore import get_datastore
import policy
from repo_manager import RepoManager
from statuses import CandidateStatus, PackageStatus

CONFIG = get_config()
REPO_MANAGER = RepoManager(CONFIG)
UI_DIR = os.path.join(BASE_DIR, "ui")
ANSI_RE = re.compile(r'\033\[[0-9;]*m')


class LogStreamBuffer:
    """Output of the currently running CLI action. Only one action runs at a time."""

    def __init__(self, max_lines=1000):
        self.lock = threading.Lock()
        self.lines = collections.deque(maxlen=max_lines)
        self.is_running = False
        self.current_action = "IDLE"
        self.last_status = "IDLE"

    def write_line(self, line):
        with self.lock:
            self.lines.append(line)

    def get_logs(self):
        with self.lock:
            return "".join(self.lines)

    def try_start(self, action_name) -> bool:
        """Atomically claims the single action slot."""
        with self.lock:
            if self.is_running:
                return False
            self.is_running = True
            self.current_action = action_name
            self.lines.clear()
            return True

    def finish(self, status):
        with self.lock:
            self.is_running = False
            self.current_action = "IDLE"
            self.last_status = status


GLOBAL_BUFFER = LogStreamBuffer()

_GIT_DIFF_CACHE = {"stat": "", "timestamp": 0.0}


def get_cached_git_diff_stat(ttl: float = 5.0) -> str:
    now = time.time()
    if now - _GIT_DIFF_CACHE["timestamp"] > ttl:
        _GIT_DIFF_CACHE["stat"] = REPO_MANAGER.get_diff_stat()
        _GIT_DIFF_CACHE["timestamp"] = now
    return _GIT_DIFF_CACHE["stat"]


def execute_cli_action(cmd_args, action_name):
    """Runs run_updater.py with cmd_args, streaming output into GLOBAL_BUFFER."""
    GLOBAL_BUFFER.write_line(f"\n>>> [{time.strftime('%H:%M:%S')}] STARTING ACTION: {action_name} <<<\n")
    env = os.environ.copy()
    env["PYTHONUNBUFFERED"] = "1"
    env["PYTHONPATH"] = f"{BASE_DIR}:" + env.get("PYTHONPATH", "")
    status = "ERROR"
    try:
        proc = subprocess.Popen(
            [sys.executable, "-u", os.path.join(BASE_DIR, "run_updater.py")] + cmd_args,
            cwd=BASE_DIR,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
            env=env
        )
        for line in iter(proc.stdout.readline, ''):
            GLOBAL_BUFFER.write_line(ANSI_RE.sub('', line))
        proc.stdout.close()
        return_code = proc.wait()
        if return_code == 0:
            status = "SUCCESS"
            GLOBAL_BUFFER.write_line(f"\n>>> [{time.strftime('%H:%M:%S')}] COMPLETED SUCCESSFULLY <<<\n")
        else:
            status = "FAILED"
            GLOBAL_BUFFER.write_line(f"\n>>> [{time.strftime('%H:%M:%S')}] FAILED WITH CODE {return_code} <<<\n")
    except Exception as e:
        GLOBAL_BUFFER.write_line(f"\n>>> [ERROR] Exception executing {action_name}: {e} <<<\n")
    finally:
        _GIT_DIFF_CACHE["timestamp"] = 0.0
        GLOBAL_BUFFER.finish(status)


# ------------------------------------------------------------------ poller

def background_poller(stop_event: threading.Event):
    """
    Keeps PR statuses and Cloud Build test results fresh without holding the action slot.
    All state lives in the DataStore, so monitoring resumes automatically after a restart.
    Ticks are skipped while a CLI action runs, to avoid racing its writes.
    """
    from test_manager import get_test_manager
    pr_interval = CONFIG.server.pr_sync_interval_seconds
    test_interval = CONFIG.server.test_poll_interval_seconds
    next_pr = next_test = 0.0
    print(f"[Poller] Started (PR sync every {pr_interval}s, test poll every {test_interval}s).", flush=True)

    while not stop_event.wait(5):
        if GLOBAL_BUFFER.is_running:
            continue
        now = time.time()
        store = get_datastore()
        if now >= next_pr:
            next_pr = now + pr_interval
            try:
                res = REPO_MANAGER.sync_open_pr_statuses(store)
                if res.get("changes"):
                    print(f"[Poller] PR sync applied {len(res['changes'])} change(s).", flush=True)
            except Exception as e:
                print(f"[Poller] [WARN] PR sync failed: {e}", flush=True)
        if now >= next_test:
            next_test = now + test_interval
            try:
                get_test_manager().poll_testing_candidates(store)
            except Exception as e:
                print(f"[Poller] [WARN] Test poll failed: {e}", flush=True)


# ------------------------------------------------------------ direct actions

def action_blueprint_selection(store, pkg_id, data):
    return store.update_blueprint_selection(
        package_id=pkg_id,
        action=data["action"],
        instance_id=data.get("instance_id"),
        enabled=data.get("enabled"),
    )


# Synchronous DataStore actions (require package_id): (store, package_id, request body) -> result.
DIRECT_ACTIONS = {
    "snooze": lambda store, pkg_id, data: policy.snooze(store, pkg_id, days=data.get("days"), version=data.get("version")),
    "block": lambda store, pkg_id, data: policy.block(store, pkg_id, version=data.get("version")),
    "unblock": lambda store, pkg_id, data: policy.unblock(store, pkg_id),
    "toggle_blueprint": action_blueprint_selection,
    "select_all_blueprints": action_blueprint_selection,
    "deselect_all_blueprints": action_blueprint_selection,
}

# CLI actions: action -> (args builder, display name, requires package_id).
# Test monitoring is owned by the background poller, so the CLI never waits on tests.
CLI_ACTIONS = {
    "end_to_end": (lambda p: ["--end-to-end", "--no-wait-test"], lambda p: "Full Pipeline Run", False),
    "check_all": (lambda p: ["--check-all"], lambda p: "Source Qualification Agent", False),
    "test_rule_blocking": (lambda p: ["--rules"], lambda p: "Learned Rule Policy Evaluation", False),
    "apply": (lambda p: ["--apply", p, "--no-wait-test"], lambda p: f"Orchestrator Agent Update ({p})", True),
    "test": (lambda p: ["--test", p, "--no-wait-test"], lambda p: f"Trigger Tests ({p})", True),
    "reset": (lambda p: ["--reset"], lambda p: "Reset Environment & State", False),
}


class DashboardHandler(SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=UI_DIR, **kwargs)

    def end_headers(self):
        self.send_header("Cache-Control", "no-store, no-cache, must-revalidate, max-age=0")
        super().end_headers()

    def _send_json(self, code, payload):
        body = json.dumps(payload).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path == "/api/state":
            self.handle_get_state()
        elif self.path.startswith("/api/logs"):
            self._send_json(200, {
                "logs": GLOBAL_BUFFER.get_logs(),
                "is_running": GLOBAL_BUFFER.is_running,
                "current_action": GLOBAL_BUFFER.current_action,
                "last_status": GLOBAL_BUFFER.last_status
            })
        elif self.path.startswith("/api/diff"):
            self._send_json(200, {"diff": REPO_MANAGER.get_diff()})
        else:
            super().do_GET()

    def do_POST(self):
        if self.path == "/api/action":
            self.handle_post_action()
        else:
            self._send_json(404, {"error": "Endpoint not found"})

    def handle_get_state(self):
        try:
            store = get_datastore()
            packages = store.list_packages()
            instances = store.list_all_blueprints()
            rules = store.list_rules()
            candidates = store.list_candidates()

            def _count_pkgs(pkg_status, cand_status, version_field):
                ids = {p["package_id"] for p in packages if p.get("status") == pkg_status or p.get(version_field)}
                ids |= {c["package_id"] for c in candidates if c.get("status") == cand_status}
                return len(ids)

            self._send_json(200, {
                "packages": packages,
                "instances": instances,
                "rules": rules,
                "candidates": candidates,
                "has_modifications": bool(get_cached_git_diff_stat()),
                "is_running": GLOBAL_BUFFER.is_running,
                "current_action": GLOBAL_BUFFER.current_action,
                "last_status": GLOBAL_BUFFER.last_status,
                "config": {
                    "project_id": CONFIG.database.project_id,
                    "cloud_build_project_id": CONFIG.cloud_build.project_id,
                    "owner": CONFIG.repository.owner,
                    "repo_name": CONFIG.repository.name,
                    "base_branch": CONFIG.repository.base_branch,
                    "is_fork": CONFIG.repository.is_fork,
                    "fork_owner": CONFIG.repository.fork_owner,
                    "snooze_days_default": CONFIG.policy.default_snooze_days,
                    "trigger_prefix": CONFIG.cloud_build.trigger_prefix,
                },
                "stats": {
                    "total_packages": len(packages),
                    "total_instances": len(instances),
                    "total_rules": len(rules),
                    "pending_updates": sum(1 for c in candidates if c.get("status") == CandidateStatus.UPDATE_FOUND),
                    "ready_updates": sum(1 for c in candidates if c.get("status") == CandidateStatus.READY_FOR_REVIEW),
                    "snoozed_packages": _count_pkgs(PackageStatus.SNOOZED, CandidateStatus.SNOOZED, "snoozed_version"),
                    "blocked_packages": _count_pkgs(PackageStatus.BLOCKED, CandidateStatus.BLOCKED, "blocked_version"),
                }
            })
        except Exception as e:
            import traceback
            traceback.print_exc()
            self._send_json(500, {"error": str(e)})

    def handle_post_action(self):
        # Requiring a JSON content type forces a CORS preflight for cross-site requests,
        # which this server never approves, so other origins cannot trigger actions.
        if not self.headers.get("Content-Type", "").startswith("application/json"):
            self._send_json(415, {"error": "Content-Type must be application/json"})
            return
        try:
            length = int(self.headers.get("Content-Length", 0))
            data = json.loads(self.rfile.read(length).decode("utf-8") or "{}")
        except (ValueError, UnicodeDecodeError):
            self._send_json(400, {"error": "Invalid JSON body"})
            return

        action = data.get("action")
        pkg_id = data.get("package_id")

        if action in DIRECT_ACTIONS:
            if not pkg_id:
                self._send_json(400, {"error": f"package_id required for {action}"})
                return
            store = get_datastore()
            if not store.get_package(pkg_id):
                self._send_json(404, {"error": f"Package '{pkg_id}' not found"})
                return
            self._send_json(200, DIRECT_ACTIONS[action](store, pkg_id, data))
            return

        if action not in CLI_ACTIONS:
            self._send_json(400, {"error": f"Unknown action: {action}"})
            return
        build_args, build_name, needs_pkg = CLI_ACTIONS[action]
        if needs_pkg and not pkg_id:
            self._send_json(400, {"error": f"package_id required for {action}"})
            return

        action_name = build_name(pkg_id)
        if not GLOBAL_BUFFER.try_start(action_name):
            self._send_json(409, {"error": "Another action is currently running"})
            return
        threading.Thread(target=execute_cli_action, args=(build_args(pkg_id), action_name), daemon=True).start()
        self._send_json(200, {"status": "STARTED", "action": action_name})


class DualStackHTTPServer(ThreadingHTTPServer):
    """Accepts both IPv4 and IPv6 (e.g. port forwarders that connect to ::1)."""
    address_family = socket.AF_INET6

    def server_bind(self):
        self.socket.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY, 0)
        super().server_bind()


def run_server(port=None):
    port = port or CONFIG.server.port
    host = CONFIG.server.host

    if host in ("", "0.0.0.0", "::") and socket.has_ipv6:
        server = DualStackHTTPServer(("::", port), DashboardHandler)
    else:
        server = ThreadingHTTPServer((host, port), DashboardHandler)

    try:
        REPO_MANAGER.ensure_workspace(force_clean=True)
    except Exception as e:
        print(f"[Server] [WARN] Initial workspace sync error: {e}", flush=True)

    stop_event = threading.Event()
    threading.Thread(target=background_poller, args=(stop_event,), daemon=True, name="poller").start()
    print("\n======================================================================")
    print("  CLUSTER TOOLKIT UPDATER - WEB DASHBOARD SERVER")
    print("======================================================================")
    print(f"  Target Repo:    {CONFIG.repository.url} ({CONFIG.repository.branch})")
    print(f"  Listening on:   http://{host}:{port}")
    print(f"  Local Access:   http://localhost:{port}")
    print("======================================================================\n", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nShutting down server...")
    finally:
        stop_event.set()
        server.server_close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=CONFIG.server.port, help=f"Port to bind server (default: {CONFIG.server.port})")
    args = parser.parse_args()
    run_server(args.port)
