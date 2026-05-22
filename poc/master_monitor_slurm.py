# Copyright 2026 Google LLC
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

# poc/master_monitor_slurm.py
import subprocess
import time
import threading
import sys
import os

PROJECT_ID = os.environ.get('PROJECT_ID')
CONTROLLER_NAME = os.environ.get('SLURM_CONTROLLER') # e.g., "rahim-static-210526-controller"
ZONE = os.environ.get('SLURM_ZONE', 'us-central1-a')

TRIGGER_1 = "poc-test-infra-master-trigger"
TRIGGER_2 = "poc-test-infra-master-trigger-2"

# Simple terminal colors
CYAN = "\033[96m"
YELLOW = "\033[93m"
GREEN = "\033[92m"
RESET = "\033[0m"

def run_remote_cmd(cmd_str):
    """Helper to run a command on the Slurm Controller VM over GCP SSH."""
    if not CONTROLLER_NAME:
        # Fallback to local execution if not specified (for testing on login node)
        result = subprocess.run(cmd_str.split(), capture_output=True, text=True)
        return result
        
    gcloud_cmd = [
        "gcloud",
        "--quiet",
        "compute",
        "ssh",
        CONTROLLER_NAME,
        f"--zone={ZONE}",
        f"--project={PROJECT_ID}",
        "--tunnel-through-iap",
        "--command",
        cmd_str
    ]
    return subprocess.run(gcloud_cmd, capture_output=True, text=True)

def submit_job(name, license_name, trigger_name):
    # Slurm command to run on the remote controller (No YAML file needed!)
    slurm_cmd = f"sbatch --parsable --job-name={name} --licenses={license_name}:1 /home/poc/run_test_slurm.sh {trigger_name}"
    print(f"Submitting job to Slurm via SSH: {slurm_cmd}")
    
    result = run_remote_cmd(slurm_cmd)
    if result.returncode != 0:
        print(f"Error submitting job to Slurm: {result.stderr}")
        sys.exit(1)
        
    job_id = result.stdout.strip().splitlines()[-1].strip()
    print(f"Successfully submitted Job {job_id}")
    return job_id

def get_job_info(job_id):
    # Query squeue remotely for state, reason, and name
    squeue_cmd = f"squeue --job {job_id} --noheader --format=%T,%r,%j"
    result = run_remote_cmd(squeue_cmd)
    output = result.stdout.strip()
    
    if not output:
        # Check accounting database remotely if finished
        sacct_cmd = f"sacct -j {job_id} --noheader --format=State,ExitCode"
        result_acct = run_remote_cmd(sacct_cmd)
        acct_output = result_acct.stdout.strip()
        if acct_output:
            parts = [p.strip() for p in acct_output.split() if p.strip()]
            if len(parts) >= 1:
                return parts[0], "None", "FINISHED"
        return "COMPLETED", "None", "FINISHED"
    
    parts = output.split(",")
    state = parts[0].strip()
    reason = parts[1].strip()
    
    # Query scontrol remotely to get the comment (Build ID)
    scontrol_cmd = f"scontrol show job {job_id}"
    result_desc = run_remote_cmd(scontrol_cmd)
    build_id = ""
    for line in result_desc.stdout.splitlines():
        if "Comment=" in line:
            comment_val = line.split("Comment=")[1].strip()
            build_id = comment_val.split()[0]
            break
            
    return state, reason, build_id

def stream_build_logs(prefix, color, build_id):
    print(f"{color}[{prefix}]{RESET} Starting log stream for Child Build {build_id}...")
    cmd = ["gcloud", "builds", "log", "--stream", build_id]
    process = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    
    while True:
        line = process.stdout.readline()
        if not line and process.poll() is not None:
            break
        if line:
            print(f"{color}[{prefix}]{RESET} {line.strip()}")
    
    print(f"{color}[{prefix}]{RESET} Log stream finished.")

def run_poc():
    if CONTROLLER_NAME:
        print(f"Orchestrator running in Hybrid Mode. Project: {PROJECT_ID} | Slurm Controller: {CONTROLLER_NAME} ({ZONE})")
    else:
        print("Orchestrator running in Local Mode (on Slurm Controller).")

    # 1. Submit jobs to Slurm
    job1 = submit_job("T1_Run1", TRIGGER_1, TRIGGER_1)
    job2 = submit_job("T2_Run1", TRIGGER_2, TRIGGER_2)
    job3 = submit_job("T1_Run2", TRIGGER_1, TRIGGER_1)
    
    tracked_jobs = {
        job1: {"name": "T1_Run1", "color": CYAN, "streamed": False},
        job2: {"name": "T2_Run1", "color": YELLOW, "streamed": False},
        job3: {"name": "T1_Run2", "color": GREEN, "streamed": False}
    }
    
    active_streams = []
    
    print("\nStarting queue status monitor loop...")
    while True:
        all_completed = True
        print("\n====================== QUEUE STATUS SUMMARY ======================")
        for job_id, info in tracked_jobs.items():
            state, reason, build_id = get_job_info(job_id)
            print(f"Job ID: {job_id:<6} | Name: {info['name']:<10} | State: {state:<10} | Reason: {reason:<12} | Build ID: {build_id if build_id else '-'}")
            
            if state != "COMPLETED" and state != "FAILED":
                all_completed = False
                
            # If the job is running and we have a Build ID, start streaming its logs
            if state == "RUNNING" and build_id and not info["streamed"]:
                info["streamed"] = True
                t = threading.Thread(target=stream_build_logs, args=(info["name"], info["color"], build_id))
                t.start()
                active_streams.append(t)
        print("==================================================================\n")
        
        if all_completed:
            break
            
        time.sleep(10)
        
    # Wait for all logging threads to finish
    for t in active_streams:
        t.join()
        
    print("\nProof of Concept Complete! All tests have finished successfully.")

if __name__ == "__main__":
    run_poc()
