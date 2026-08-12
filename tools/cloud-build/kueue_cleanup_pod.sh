# Trap function that runs the rescue playbook (terraform destroy)
# if the pod is terminated by Kueue, fails, or is cancelled.
# Expects variables: RUN_CLEANUP, DEPLOYMENT_NAME, ANSIBLE_PID
cleanup_pod() {
  local exit_code=$?
  trap - EXIT SIGTERM SIGINT ERR
  set +e

  if [ "${RUN_CLEANUP:-false}" = "false" ]; then
    exit $exit_code
  fi
  if [ $exit_code -eq 0 ]; then exit_code=1; fi

  echo ""
  echo "=========================================================================="
  echo "CAUGHT SIGTERM OR SCRIPT ERROR!"
  echo "Halting primary Ansible execution..."
  echo "=========================================================================="
  if [ -n "${ANSIBLE_PID:-}" ]; then
      kill -TERM $ANSIBLE_PID 2>/dev/null || true
      wait $ANSIBLE_PID 2>/dev/null || true
      echo "Waiting 15s for Terraform to release GCS backend state locks..."
      sleep 15
  fi

  echo ""
  echo "INITIATING RESCUE PLAYBOOK: Destroying leaked infrastructure for $DEPLOYMENT_NAME..."
  echo "- hosts: localhost" > /workspace/cleanup-playbook.yml
  echo "  tasks:" >> /workspace/cleanup-playbook.yml
  echo "  - ansible.builtin.include_tasks:" >> /workspace/cleanup-playbook.yml
  echo "      file: tools/cloud-build/daily-tests/ansible_playbooks/tasks/rescue_gcluster_failure.yml" >> /workspace/cleanup-playbook.yml

  ansible-playbook /workspace/cleanup-playbook.yml -e deployment_name="$DEPLOYMENT_NAME" -e workspace="/workspace" || true

  echo "Graceful cleanup finished."
  exit $exit_code
}
