#!/bin/bash
# Copyright 2026 Google LLC
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#      http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

# poc/run_test_slurm.sh
# Usage: sbatch --licenses=<trigger_name>:1 /home/poc/run_test_slurm.sh <trigger_name>

if [ -z "$1" ]; then
    echo "Usage: $0 <trigger_name>"
    exit 1
fi

TRIGGER_NAME=$1

echo "Starting Slurm Job Wrapper ($SLURM_JOB_ID) for Trigger: $TRIGGER_NAME..."

# 1. Trigger the ACTUAL pre-existing Cloud Build Trigger by name!
# (We use --format="value(metadata.build.id)" to parse the nested build ID)
BUILD_ID=$(gcloud builds triggers run $TRIGGER_NAME --branch=poc-test-infra --format="value(metadata.build.id)")

if [ -z "$BUILD_ID" ]; then
    echo "Error: Failed to trigger Cloud Build."
    exit 1
fi

echo "Cloud Build Triggered successfully. Build ID: $BUILD_ID"

# 2. Save the Build ID in the Slurm Job Comment so Master can find it
scontrol update jobid=$SLURM_JOB_ID comment="$BUILD_ID"

# 3. Block and stream the logs in real-time
gcloud builds log --stream $BUILD_ID
EXIT_CODE=$?

echo "Cloud Build completed with exit code: $EXIT_CODE"
exit $EXIT_CODE
