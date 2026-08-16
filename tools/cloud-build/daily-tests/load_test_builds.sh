#!/bin/bash
# load_test_builds.sh
# Schedules Cloud Build Triggers to run multiple times at random intervals.

PROJECT_ID="hpc-toolkit-dev-2"
# Number of times to trigger the test suite
NUM_RUNS=${1:-3}
# The window of time over which to randomize the runs (in seconds). Default: 24 hours (86400s)
MAX_DELAY_SECONDS=${2:-86400} 
# Branch to run the triggers against. Default: develop
BRANCH=${3:-develop}

echo "Fetching available triggers in $PROJECT_ID..."
# Get all triggers but exclude the cleanup scripts
TRIGGERS=$(gcloud builds triggers list --project="$PROJECT_ID" --format="value(name)" | grep -v "cleanup")
TRIGGER_COUNT=$(echo "$TRIGGERS" | wc -w | tr -d ' ')

echo "🚀 Scheduling $NUM_RUNS test runs ($TRIGGER_COUNT triggers per run) over the next $((MAX_DELAY_SECONDS / 3600)) hours on branch: $BRANCH"
echo "--------------------------------------------------------"

for i in $(seq 1 $NUM_RUNS); do
  # First run executes immediately, subsequent runs are randomized
  if [ "$i" -eq 1 ]; then
    DELAY=0
  else
    DELAY=$(shuf -i 1-${MAX_DELAY_SECONDS} -n 1)
  fi
  
  # Calculate the actual scheduled time for display purposes
  if [[ "$OSTYPE" == "darwin"* ]]; then
    SCHEDULED_TIME=$(date -r "$(( $(date +%s) + DELAY ))" "+%Y-%m-%d %H:%M:%S")
  else
    SCHEDULED_TIME=$(date -d "@$(( $(date +%s) + DELAY ))" "+%Y-%m-%d %H:%M:%S")
  fi
  
  echo "⏱️  Run $i scheduled for: $SCHEDULED_TIME (in $DELAY seconds)"
  
  # Background process that sleeps and then runs the suite
  (
    sleep $DELAY
    # Use "daily-" prefix so that testing exactly mimics the official scheduled runs
    TEST_PREFIX="daily-"
    
    for trigger in $TRIGGERS; do
      gcloud builds triggers run "$trigger" \
        --project "$PROJECT_ID" \
        --branch "$BRANCH" \
        --substitutions=_TEST_PREFIX="$TEST_PREFIX" > /dev/null 2>&1
        
      # Small sleep to prevent hitting Cloud Build API rate limits
      sleep 0.5
    done
  ) &
done

echo "--------------------------------------------------------"
echo "✅ All $NUM_RUNS background jobs have been scheduled!"
echo "You can leave this terminal running or close it (if using nohup/tmux)."
