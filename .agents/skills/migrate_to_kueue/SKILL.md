---
name: migrate-to-kueue
description: "Migrates a legacy Cloud Build test pipeline to the new GKE Kueue-based architecture. Use this skill when asked to update a test to run through the Kueue integration testing cluster."
---

# Migrate a Cloud Build Test to Kueue

This skill guides you through migrating a standard Cloud Build integration test to the new Kueue-based GKE architecture. 

## 1. Pipeline YAML Structure

Create a new file with a `-kueue.yaml` postfix (e.g., `test-name-kueue.yaml`) alongside the original file. In this new file, replace the legacy synchronous `ansible-playbook` step with a 4-step Kueue structure:

1. **Build Workspace Image**: Build and push a docker image containing the current repo codebase.
2. **Generate Job Manifest**: Generate a `Job` YAML file defining a GKE Job that executes the test runner pod. 
3. **Submit and Monitor**: Apply the Job to the `test-kueue-cluster` and monitor it using `tools/cloud-build/monitor_kueue_job.sh`. This step handles capacity exhaustion retries.
4. **Cleanup Image**: Delete the built image to save costs.

## 2. GKE Job Specification Details

When generating the Job manifest (Step 2) using a bash `cat <<EOF > job.yaml` heredoc, ensure the following requirements are met:
- **`secretEnv` Variables**: If the legacy step used `secretEnv` (e.g., `GCLUSTER_GCS_PATH`), move it to the `generate-job-manifest` step. Inject it into the Job template by referencing it as `$$ENV_VAR` in the heredoc.
- **Regular `env` Variables**: If the legacy step used standard `env` variables (e.g., `PROJECT_ID`, `BUILD_ID`, `NUM_NODES`), you MUST copy them into the Pod's `containers[0].env` array in the Job manifest. If these are omitted, scripts inside the Pod (like `find_available_zone.sh` which expects `BUILD_ID`) will fail with an `unbound variable` error!
- **`OPTIONS_BUCKET` Logic**: If the legacy pipeline dynamically determines an `OPTIONS_BUCKET` based on `PROJECT_ID` to construct `OPTIONS_GCS_PATH`, you MUST place this bash `if` statement in the `generate-job-manifest` step *before* the heredoc. Ensure you map `hpc-toolkit-gsc` to `daily-tests-gsc-tf-state` and other projects to the original bucket. Then, construct the path dynamically in the Pod's `env` array (e.g., `value: "gs://$${OPTIONS_BUCKET}/a3uoptions.txt"`).
- **Variable Escaping**: Because the pod's bash script is embedded inside a heredoc, you MUST carefully escape ALL container-side bash variables (e.g., `\$$BLUEPRINT`, `\$$DEPLOYMENT_NAME`, `\$$ANSIBLE_PID`) with `\$$` so they are evaluated dynamically inside the pod, rather than prematurely by the host shell generating the YAML. Verify each line carefully to ensure no variable misses this escaping!
- **Suspend**: `spec.suspend` must be `true` (required for Kueue to manage it).
- **Labels**: `kueue.x-k8s.io/queue-name: local-queue-test-locks`
- **Grace Period**: `terminationGracePeriodSeconds` must be at least `1800` (30 minutes) to allow terraform destroy to finish if cancelled or evicted.
- **Service Account**: Set `serviceAccountName: test-kueue-cluster-runner-ksa`
- **Priority**: Dynamically assign `priorityClassName` based on `_TEST_PREFIX` (`daily-test-priority` or `pr-test-priority`).

## 3. Container Entrypoint & Cloud Build Traps

Robust cleanup requires traps both inside the Kubernetes pod and the Cloud Build step:
- **Cloud Build Step Trap**: In the `submit-and-monitor-gke-job` step, add a `cleanup_cb()` function and trap for `SIGTERM SIGINT`. This ensures that if the Cloud Build pipeline is cancelled, the step catches the signal and runs `kubectl delete job "$$JOB_NAME"`.
- **Pod `cleanup_pod()` Trap**: Inside the container's entrypoint, catch `SIGTERM` (which is sent by Kubernetes when the Job is deleted by Cloud Build or the Reaper Pod) and execute the `rescue_gcluster_failure.yml` playbook to safely run `terraform destroy`.
- **Ansible `--user` Flag**: Because the pod runs under Workload Identity using the `test-kueue-cluster-runner` service account, any SSH operations via OS Login must use its specific Google Account unique ID. Ensure the `ansible-playbook` `--user` flag is set to `sa_116785999750372214978`.

## 4. Resource Request Locks (Concurrency)

The container `resources.requests` and `limits` are used by Kueue to guard against GCP zone capacity exhaustion. Your main goal is to enforce locks on tests that use highly-exhaustive, scarce, or expensive cloud hardware.
- **Resource-Based Locks**: If the test deploys scarce hardware (like `a3-ultragpu`, `a4-ultragpu`, `filestore-high-scale`, `megagpu`, etc.), you MUST request a resource-specific lock (e.g., `test-locks/a3-ultragpu: 1`) so that multiple tests requesting the same rare hardware do not run simultaneously and exhaust quota.
- **Test-Specific Locks**: You must also request an exact test-specific lock (e.g., `test-locks/slurm-gcp-v6-rocky8: 1` or `test-locks/gke-a3-ultragpu-onspot: 1`) to guarantee that multiple instances of the exact same test never run in parallel.

## 5. Kueue Setup Updates

If you are adding a new lock (either a new Resource-Based lock like `a4-ultragpu` or a new Test-Specific lock), you MUST register it to the cluster queue so Kueue can track it:
1. **`tools/cloud-build/dummy-device-plugin.yaml`**: Add the new device name to the args.
2. **`tools/cloud-build/kueue-setup.yaml`**: Add the resource to `coveredResources` and `flavors`. Test-specific locks should always have `nominalQuota: 1`. Resource-based locks (like `a3-ultragpu`) should have their `nominalQuota` carefully tuned based on available GCP capacity.

## 6. Pipeline Settings
- Update the top-level `timeout` to `86400s` (24hr) to account for queue waiting times.
- Ensure the `MAX_RETRIES` in the Submit and Monitor step aligns with the new timeout. With a `RETRY_DELAY` of 300 seconds, `MAX_RETRIES` should be calculated based on the timeout and then rounded down to the nearest 10 (e.g., 24 hours = 86400s / 300s = 288, so set `MAX_RETRIES=280`).

## 7. Final Validation

Before finalizing your work, you MUST validate your migrated file. A highly recommended way to do this is to review the exact line-by-line diff of the reference migration:
Compare `tools/cloud-build/daily-tests/builds/slurm-gcp-v6-rocky8.yaml` against `tools/cloud-build/daily-tests/builds/slurm-gcp-v6-rocky8-kueue.yaml`.
This reference diff serves as the gold standard. Use it to double-check that you haven't missed any subtle logic changes, variable escaping, or `secretEnv` relocations in your new pipeline!
