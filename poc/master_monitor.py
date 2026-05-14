import json
import os
import time
from google.cloud import tasks_v2
from google.cloud import cloudbuild_v1

PROJECT_ID = os.environ['PROJECT_ID']
REGION = "us-central1"
QUEUE_NAME = "poc-test-queue"
TRIGGER_NAME = "poc-test-trigger"

def enqueue_test():
    client = tasks_v2.CloudTasksClient()
    parent = client.queue_path(PROJECT_ID, REGION, QUEUE_NAME)
    
    url = f"https://cloudbuild.googleapis.com/v1/projects/{PROJECT_ID}/locations/global/triggers/{TRIGGER_NAME}:run"
    
    task = {
        "http_request": {
            "http_method": tasks_v2.HttpMethod.POST,
            "url": url,
            "oidc_token": {
                "service_account_email": f"{PROJECT_ID}@appspot.gserviceaccount.com", 
            },
        }
    }
    
    response = client.create_task(request={"parent": parent, "task": task})
    print(f"Created task {response.name}")
    return response.name

def poll_build_status():
    client = cloudbuild_v1.CloudBuildClient()
    
    print("Polling for builds triggered by queue...")
    while True:
        builds = client.list_builds(project_id=PROJECT_ID, location="global")
        
        for build in builds:
            if build.substitutions.get('_TRIGGER_NAME') == TRIGGER_NAME:
                print(f"Found build {build.id} with status: {build.status.name}")
                
                if build.status.name in ["SUCCESS", "FAILURE", "CANCELLED"]:
                    print(f"Build finished with status: {build.status.name}")
                    return build.status.name
                    
        print("No finished builds found yet. Waiting 30 seconds...")
        time.sleep(30)

if __name__ == "__main__":
    enqueue_test()
    status = poll_build_status()
    if status != "SUCCESS":
        print("Handling failure / Requeuing logic would go here.")
