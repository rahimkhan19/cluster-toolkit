---
name: migrate_to_kueue
description: Guidelines for migrating Cloud Build tests to Kueue.
---

# Kueue Migration Guidelines

When migrating Cloud Build steps to Kueue pods, observe the following rules for variable escaping inside `args` bash heredocs:

1. **Cloud Build default substitutions** (like `$PROJECT_ID`, `$BUILD_ID`) used in the Job `env` values must use a single `$` (e.g., `value: "$PROJECT_ID"`) so they are substituted by Cloud Build before the bash heredoc generates the YAML.
2. **Host shell variables** defined within the Cloud Build step script (like `BUILD_ID_SHORT`) must be escaped with `$$` (e.g., `$$BUILD_ID_SHORT`) so the host bash shell evaluates them when writing the YAML.
3. **Pod-side bash variables** (used in the `args` of the container bash script) must be escaped with `\$` (e.g., `\$${ZONE:-}` or `\$${ZONE%-*}`) so they are passed cleanly through Cloud Build and the host bash heredoc, allowing them to be evaluated at runtime inside the pod.
   - **WARNING:** Do NOT use `\$$${VAR}` as it will evaluate to `\$` followed by the result of bash heredoc attempting to evaluate `${VAR}` locally, completely breaking the pod script.
