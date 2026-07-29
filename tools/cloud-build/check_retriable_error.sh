#!/bin/bash
# Returns 0 if a retriable error (e.g. Terraform capacity error) is found in the log, 1 otherwise.
# This script is called by Kueue pipelines to determine if the job should be requeued.

LOG_FILE=$1
if [ -z "$LOG_FILE" ]; then
    echo "Usage: $0 <log_file>" >&2
    exit 2
fi

# Define all retriable errors here.
# Note: "Couldn't find a zone to deploy" and "ERROR: ZONE not found" are not included here
# because find_available_zone.sh now internally loops and waits for zone capacity to maintain Kueue locks.
RETRIABLE_ERRORS="ZONE_RESOURCE_POOL_EXHAUSTED|does not have enough resources available|not enough resources available|stockout|os-login.*ssh-keys.*add|resourceInUseByAnotherResource|Error acquiring the state lock|412 Precondition Failed|conditionNotMet|RATE_LIMIT_EXCEEDED|Mutate requests per minute|429"

if grep -q -i -E "$RETRIABLE_ERRORS" "$LOG_FILE"; then
    exit 0
else
    exit 1
fi
