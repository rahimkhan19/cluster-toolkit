# Automated Driver & Infrastructure Updater

**Bug:** [b/562019559](https://b.corp.google.com/issues/562019559) · **Location:** `tools/infra_updater/`

Keeps the driver, toolkit and image versions pinned in Cluster Toolkit blueprints up to date. For each
package it finds the newest qualified upstream release, rewrites every blueprint that pins the package,
opens a pull request, runs the blueprints' Cloud Build integration tests, and tracks the PR until it
merges. A web dashboard shows the state and drives every step.

See [END_TO_END_FLOW.md](END_TO_END_FLOW.md) for the detailed flow.

## How it works

1. **Registry.** [`packages.yaml`](packages.yaml) is the only package-specific input. Each package
   declares its upstream source (`upstream.type` / `upstream.url`), fetcher options, and **pins**:
   regexes whose named groups (`version`, `major_minor`, `url`, `filename`, `image`) locate the
   version in blueprint text.
2. **Discovery.** Every check scans `examples/**` and `community/examples/**` in a clone of the
   target repository (`target_repo/`) with those pins. Each match is a blueprint instance that records
   its own pinned version. A package's current version is the **oldest** version across its selected
   blueprints; the UI flags packages whose blueprints disagree ("⚠ out of sync").
3. **Qualification** (`--check-all`, packages in parallel). A registered fetcher lists upstream
   releases; a deterministic prefilter keeps newer, non-pre-release versions; Gemini picks the
   release with one shared prompt (`prompts/select_release.txt`), falling back to a deterministic pick
   tagged "not LLM-verified" if the LLM is unavailable. Gates: newer → GA → snooze/block policy →
   learned rules → no open PR for it → artifact URL is live. A passing release becomes an
   `UPDATE_FOUND` candidate.
4. **Apply** (`--apply <package>`). Branches off the base branch, rewrites every pin in the selected
   blueprints (each image keeps its own variant), validates the YAML, commits, pushes, opens a PR, and
   approves the PR's Cloud Build test builds (mapped from `tools/cloud-build/daily-tests/tests/*.yml`,
   plus `test_overrides` in `packages.yaml`).
5. **Tracking.** The dashboard server's background poller syncs PR states (merged → `UP_TO_DATE`,
   closed → back to `UPDATE_FOUND`) and Cloud Build results (tests can run for about a day).

State lives in Cloud Firestore (or a local JSON file, `database.provider: json`).

## Modules

| File | Role |
| :--- | :--- |
| `packages.yaml`, `registry.py` | Package registry, blueprint discovery, pin-based rewrite |
| `fetchers.py`, `selector.py`, `prompts/` | Upstream release listing per source type; release selection |
| `source_agent.py` | Qualification gates and candidate creation |
| `code_modifier.py` | Applies a candidate: rewrite, commit, push, PR, tests |
| `repo_manager.py`, `http_client.py` | Git workspace and GitHub API |
| `test_manager.py` | Blueprint → test mapping, Cloud Build approval and polling |
| `datastore.py`, `init_db.py` | Firestore / JSON state; seeding from the registry and discovery |
| `statuses.py`, `policy.py`, `versions.py` | Status vocabulary, snooze/block, version helpers |
| `config.py`, `config.yaml` | Settings (environment variables override `config.yaml`) |
| `run_updater.py` | CLI |
| `server.py`, `ui/` | Dashboard server, REST API and background poller |

## Adding a package

Add an entry to `packages.yaml` with an `upstream` type that `fetchers.py` supports
(`github_release`, `apt_repository`, `docker_hub`, `archive_scraper`, `raw_manifest`, `mft_api`) and
one or more pins. Run `python3 init_db.py --refresh` (or any check); the blueprints that match the pins
are discovered automatically. No code change is needed.

## Usage

Run from `tools/infra_updater/`. The GitHub token is read from `GITHUB_TOKEN` (or Secret Manager
secret `infra-updater-github-token`); Firestore and Vertex AI use Application Default Credentials.

```bash
python3 server.py --port 8080             # dashboard at http://localhost:8080
python3 run_updater.py --check-all        # qualify every package
python3 run_updater.py --apply gve-dkms   # apply a candidate: branch, PR, tests
python3 run_updater.py --test gve-dkms    # re-run the tests of an open PR
python3 run_updater.py --snooze spack --days 14   # also --block / --unblock
python3 run_updater.py --sync-prs         # sync PR states once (the server does this continuously)
python3 run_updater.py --end-to-end       # check, then apply every candidate
python3 run_updater.py --show-config      # also --show-tables, --rules
python3 init_db.py --refresh              # re-discover blueprints, keep runtime state
python3 run_updater.py --reset            # reset workspace and state to the baseline
```

`app.yaml` (App Engine) and `Dockerfile` (Cloud Run) deploy the dashboard; keep exactly one
always-on instance so the background poller keeps running.
