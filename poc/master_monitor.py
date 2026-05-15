import json
import os
import time
from google.cloud import tasks_v2
from google.cloud.devtools import cloudbuild_v1

PROJECT_ID = os.environ['PROJECT_ID']
REGION = "us-central1"
QUEUE_NAME = "poc-test-queue"
TRIGGER_NAME = "poc-test-infra-master-trigger"

def enqueue_test():
    print("Entering enqueue_test...")
    print("Initializing CloudTasksClient...")
    client = tasks_v2.CloudTasksClient()
    print("CloudTasksClient initialized.")
    
    print("Initializing CloudBuildClient to find trigger ID...")
    cb_client = cloudbuild_v1.CloudBuildClient()
    cb_parent = f"projects/{PROJECT_ID}/locations/global"
    
    try:
        request = cloudbuild_v1.ListBuildTriggersRequest(parent=cb_parent)
        triggers = cb_client.list_build_triggers(request=request)
        trigger_id = None
        for t in triggers:
            if t.name == TRIGGER_NAME:
                trigger_id = t.id
                break
                
        if not trigger_id:
            raise Exception(f"Trigger '{TRIGGER_NAME}' not found.")
            
        print(f"Found trigger ID: {trigger_id} for name: {TRIGGER_NAME}")
        
    except Exception as e:
        print(f"Error finding trigger: {e}")
        raise e

    parent = client.queue_path(PROJECT_ID, REGION, QUEUE_NAME)
    
    url = f"https://cloudbuild.googleapis.com/v1/projects/{PROJECT_ID}/locations/global/triggers/{trigger_id}:run"
    
    task = {
        "http_request": {
            "http_method": tasks_v2.HttpMethod.POST,
            "url": url,
            "body": b"{}",
            "oauth_token": {
                "service_account_email": f"{PROJECT_ID}@appspot.gserviceaccount.com",
                "scope": "https://www.googleapis.com/auth/cloud-platform"
            },
        }
    }
    
    print("Sending create_task request to Cloud Tasks...")
    try:
        response = client.create_task(request={"parent": parent, "task": task}, timeout=10)
        print(f"Created task {response.name}")
        return response.name
    except Exception as e:
        print(f"Error creating task: {e}")
        raise e

def poll_build_status():
    client = cloudbuild_v1.CloudBuildClient()
    parent = f"projects/{PROJECT_ID}/locations/global"
    
    print("Polling for builds triggered by queue...")
    while True:
        request = cloudbuild_v1.ListBuildsRequest(
            parent=parent,
            page_size=10
        )
        try:
            builds = client.list_builds(request=request)
            
            for build in builds:
                if build.substitutions.get('_TRIGGER_NAME') == TRIGGER_NAME:
                    print(f"Found build {build.id} with status: {build.status.name}")
                    
                    if build.status.name in ["SUCCESS", "FAILURE", "CANCELLED"]:
                        print(f"Build finished with status: {build.status.name}")
                        return build.status.name
                        
            print("No finished builds found yet. Waiting 60 seconds...")
            time.sleep(60)
        except Exception as e:
            if "429" in str(e) or "Quota exceeded" in str(e):
                print(f"Rate limit exceeded or quota issues: {e}. Waiting 60 seconds before retry...")
                time.sleep(60)
            else:
                print(f"Unexpected error: {e}")
                raise e

if __name__ == "__main__":
    enqueue_test()
    status = poll_build_status()
    if status != "SUCCESS":
        print("Handling failure / Requeuing logic would go here.")
