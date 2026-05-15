import functions_framework
import json
import os
import time
import requests
from google.cloud.devtools import cloudbuild_v1
from google.auth import default
from google.auth.transport.requests import Request

@functions_framework.http
def coordinate_build(request):
    print(f"Received raw data: {request.data}")
    try:
        request_json = json.loads(request.data.decode('utf-8'))
        print(f"Parsed JSON: {request_json}")
    except Exception as e:
        print(f"Failed to parse JSON: {e}")
        return f"Invalid JSON: {e}", 400
        
    if not request_json or 'trigger_name' not in request_json:
        print("Missing trigger_name in payload")
        return "Missing trigger_name in payload", 400

    trigger_name = request_json['trigger_name']
    project_id = os.environ.get('PROJECT_ID')
    
    cb_client = cloudbuild_v1.CloudBuildClient()
    parent = f"projects/{project_id}/locations/global"
    
    print(f"Coordinator received request for {trigger_name}")

    # 1. Check if another instance of this trigger is running
    try:
        filter_str = f'substitutions._TRIGGER_NAME="{trigger_name}"'
        req = cloudbuild_v1.ListBuildsRequest(parent=parent, page_size=10, filter=filter_str)
        builds = cb_client.list_builds(request=req)
        
        for build in builds:
            if build.status.name in ["QUEUED", "WORKING"]:
                print(f"Conflict found: Build {build.id} is in state {build.status.name} for {trigger_name}")
                return "Conflict: Build already running or queued", 503
                
    except Exception as e:
        if "429" in str(e) or "Quota exceeded" in str(e):
            print(f"Rate limit exceeded in function: {e}. Returning 503 to retry.")
            return "Rate limit exceeded", 503
        print(f"Error checking builds: {e}")
        return f"Error checking builds: {e}", 500

    # 2. Find trigger ID
    try:
        req_triggers = cloudbuild_v1.ListBuildTriggersRequest(parent=parent)
        triggers = cb_client.list_build_triggers(request=req_triggers)
        trigger_id = None
        for t in triggers:
            if t.name == trigger_name:
                trigger_id = t.id
                break
                
        if not trigger_id:
             return f"Trigger {trigger_name} not found", 404
             
    except Exception as e:
        print(f"Error finding trigger: {e}")
        return f"Error finding trigger: {e}", 500

    # 3. Trigger the build via Google API Discovery Client
    from googleapiclient.discovery import build
    
    try:
        print(f"Triggering build for {trigger_name} via Discovery API...")
        with build("cloudbuild", "v1") as cloudbuild:
            run_trigger_request = cloudbuild.projects().triggers().run(
                projectId=project_id,
                triggerId=trigger_id,
                body={
                    "branchName": "poc-test-infra",
                    "substitutions": {
                        "_TRIGGER_NAME": trigger_name
                    }
                }
            )
            response = run_trigger_request.execute()
            print(f"Successfully triggered build for {trigger_name}: {response}")
            return "Build triggered", 200
            
    except Exception as e:
        print(f"Failed to trigger build via Discovery API: {e}")
        return f"Failed to trigger build: {e}", 500
