#!/bin/bash
# Wrapper around find_available_zone.sh that loops instead of exiting when out of capacity.
# Since find_available_zone.sh uses 'exit 1', we must run it in a subshell
# to prevent it from killing the pod.

while true; do
    # Run the script in a subshell, capturing stdout and stderr.
    # To extract the exported variables, we have the subshell write them to a file.
    OUTPUT=$(
        source /workspace/tools/cloud-build/find_available_zone.sh 2>&1
        # If it succeeds, these lines will execute and save the exports.
        echo "export ZONE=${ZONE}" > /tmp/zone_export.sh
        echo "export PROVISIONING_MODEL=${PROVISIONING_MODEL}" >> /tmp/zone_export.sh
    )
    EXIT_CODE=$?
    
    # Print the output so logs still show the progress (e.g. "INFO: Trying provisioning model...")
    echo "$OUTPUT"
    
    if [ $EXIT_CODE -eq 0 ]; then
        source /tmp/zone_export.sh
        break
    else
        # Check if the failure was specifically due to zone capacity
        if echo "$OUTPUT" | grep -q "Couldn't find a zone to deploy"; then
            echo "--- RETRYING in 5 minutes to maintain queue position... ---" >&2
            sleep 300
        else
            echo "--- FATAL ERROR: find_available_zone.sh failed due to a configuration or system error. Exiting. ---" >&2
            exit 1
        fi
    fi
done
