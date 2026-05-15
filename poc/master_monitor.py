import json
import os
import time
import sys
from google.cloud import tasks_v2
from google.cloud.devtools import cloudbuild_v1

PROJECT_ID = os.environ['PROJECT_ID']
REGION = "us-central1"

# We assume these two triggers exist or will be created
TRIGGER_1 = "poc-test-infra-master-trigger"
TRIGGER_2 = "poc-test-infra-master-trigger-2"

# We assume these two queues exist or will be created
QUEUE_1 = "poc-test-queue"
QUEUE_2 = "poc-test-queue-2"

def get_trigger_id(cb_client, project_id, trigger_name):
    cb_parent = f"projects/{project_id}/locations/global"
    try:
        request = cloudbuild_v1.ListBuildTriggersRequest(parent=cb_parent)
        triggers = cb_client.list_build_triggers(request=request)
        for t in triggers:
            if t.name == trigger_name:
                return t.id
        return None
    except Exception as e:
        print(f"Error finding trigger {trigger_name}: {e}")
        return None

def enqueue_test(trigger_name, queue_name):
    client = tasks_v2.CloudTasksClient()
    cb_client = cloudbuild_v1.CloudBuildClient()
    
    print(f"Resolving ID for trigger: {trigger_name}")
    trigger_id = get_trigger_id(cb_client, PROJECT_ID, trigger_name)
    if not trigger_id:
        print(f"Error: Trigger '{trigger_name}' not found.")
        return None
        
    parent = client.queue_path(PROJECT_ID, REGION, queue_name)
    url = "https://us-central1-hpc-toolkit-dev.cloudfunctions.net/poc-coordinator-test-infra"
    
    payload = {
        "trigger_name": trigger_name
    }
    body_bytes = json.dumps(payload).encode('utf-8')
    
    task = {
        "http_request": {
            "http_method": tasks_v2.HttpMethod.POST,
            "url": url,
            "body": body_bytes,
            "headers": {
                "Content-Type": "application/json"
            },
            "oidc_token": {
                "service_account_email": f"{PROJECT_ID}@appspot.gserviceaccount.com", 
            },
        }
    }
    
    print(f"Sending create_task request for {trigger_name} to {queue_name}...")
    try:
        response = client.create_task(request={"parent": parent, "task": task}, timeout=10)
        print(f"Created task {response.name} in {queue_name}")
        return response.name
    except Exception as e:
        print(f"Error creating task for {trigger_name}: {e}")
        return None

def poll_status(trigger_names, queue_names):
    cb_client = cloudbuild_v1.CloudBuildClient()
    tasks_client = tasks_v2.CloudTasksClient()
    
    parent = f"projects/{PROJECT_ID}/locations/global"
    
    print("Starting status polling...")
    while True:
        status_map = {name: "UNKNOWN" for name in trigger_names}
        
        # 1. Check Cloud Tasks for waiting tasks in ALL queues
        for queue_name in queue_names:
            queue_parent = tasks_client.queue_path(PROJECT_ID, REGION, queue_name)
            try:
                tasks = tasks_client.list_tasks(parent=queue_parent)
                for task in tasks:
                    url = task.http_request.url
                    for name in trigger_names:
                        # If trigger name is in URL or we can map it
                        pass
            except Exception as e:
                print(f"Error listing tasks in {queue_name}: {e}")
            
        # 2. Check Cloud Build for running/finished builds
        try:
            request = cloudbuild_v1.ListBuildsRequest(parent=parent, page_size=20)
            builds = cb_client.list_builds(request=request)
            
            for build in builds:
                for name in trigger_names:
                    if build.substitutions.get('_TRIGGER_NAME') == name or name in build.id:
                         status_map[name] = build.status.name
        except Exception as e:
             if "429" in str(e):
                 print("Rate limit hit, waiting...")
                 time.sleep(60)
                 continue
             print(f"Error listing builds: {e}")
             
        # Print Summary
        print("\n--- Status Summary ---")
        for name, status in status_map.items():
            print(f"Test: {name} -> State: {status}")
        print("----------------------")
        
        # Check if all are finished
        all_finished = all(status in ["SUCCESS", "FAILURE", "CANCELLED"] for status in status_map.values())
        if all_finished and len(status_map) > 0:
            print("All tracked builds have finished.")
            break
            
        time.sleep(60)

if __name__ == "__main__":
    # Enqueue tests as requested by user: T1, T2, T1
    print("Enqueuing TRIGGER_1 to QUEUE_1...")
    enqueue_test(TRIGGER_1, QUEUE_1)
    
    print("Enqueuing TRIGGER_2 to QUEUE_2...")
    enqueue_test(TRIGGER_2, QUEUE_2)
    
    print("Enqueuing TRIGGER_1 to QUEUE_1 again...")
    enqueue_test(TRIGGER_1, QUEUE_1)
    
    # Poll status for both
    poll_status([TRIGGER_1, TRIGGER_2], [QUEUE_1, QUEUE_2])
