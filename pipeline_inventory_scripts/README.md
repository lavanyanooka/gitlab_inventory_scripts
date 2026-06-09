# Pipeline Inventory (`fetch_gitlab_pipelines.py`)

Walks a GitLab group recursively (all nested subgroups, any depth) and
writes a single CSV containing every pipeline asset found across projects.

Parallel, multi-token, rate-limit-aware, resume-safe.

## Install

Install the shared dependencies once from the workspace root:

```powershell
pip install -r ..\requirements.txt
```

Python 3.10+. Token needs `read_api` (and `read_repository` for private
repos).

## Quick start

The recommended way is a `.env` file at the workspace root (copy
`../.env-example` and fill in your values). Both scripts auto-load it.

```powershell
# From the workspace root, once:
Copy-Item ..\.env-example ..\.env
notepad ..\.env       # set GITLAB_TOKEN (and optionally GITLAB_GROUP)
```

Then run from this folder:

```powershell
# FULL inventory — every project, every branch, pipeline runs, CI configs,
# reusable templates, deployments, and environments.
python fetch_gitlab_pipelines.py --group your-group --full

# Default scope: everything on the default branch + pipeline runs from all
# branches (the API returns all refs unless you filter).
python fetch_gitlab_pipelines.py --group your-group

# Inventory only, no run history (much faster)
python fetch_gitlab_pipelines.py --group your-group --no-runs

# Scan ci_config + templates on EVERY branch (not just default)
python fetch_gitlab_pipelines.py --group your-group --all-branches

# Add CD visibility (deployments + environments)
python fetch_gitlab_pipelines.py --group your-group `
    --with-deployments --with-environments

# Skip the YAML template scan if it's slow on monorepos
python fetch_gitlab_pipelines.py --group your-group --no-templates

# Reuse the project list from the repo inventory (skips group listing)
python fetch_gitlab_pipelines.py `
    --projects-csv ..\repo_inventory_scripts\data\gitlab-stats.csv --full

# Multiple PATs in round-robin (each PAT should belong to a different
# user for real Nx throughput — see top-level README)
$env:GITLAB_TOKENS = "glpat-a,glpat-b,glpat-c"
python fetch_gitlab_pipelines.py --group your-group --workers-per-token 4 --full

# Self-managed GitLab
python fetch_gitlab_pipelines.py --group your-group `
    --gitlab-url https://gitlab.example.com
```

Shell environment variables still work and override anything in `.env`:

```powershell
$env:GITLAB_TOKEN  = "glpat-xxxxxxxxxxxx"
$env:GITLAB_TOKENS = "glpat-a,glpat-b,glpat-c"   # pool mode
python fetch_gitlab_pipelines.py --group your-group
```

## Output

A single CSV at `output/pipelines.csv` (or `--output PATH`), with columns:

```
type, pipeline_id, group, subgroup, project, branch, status, source,
sha, file_path, file_ref, created_at, updated_at, web_url,
project_web_url, project_id, environment
```

Filter on the `type` column in Excel to switch views:

| `type` | What the row represents | Enabled by |
|---|---|---|
| `pipeline_run` | An actual pipeline execution — any branch/tag, any status | default (unless `--no-runs`) |
| `ci_config` | The project's `.gitlab-ci.yml` (or custom `ci_config_path`, including external `path@group/project:ref`) | default branch only; `--all-branches` for every branch |
| `reusable_template` | A `*.yml` / `*.yaml` file under `templates/`, `ci-templates/`, or `ci/` | default branch only; `--all-branches` for every branch (unless `--no-templates`) |
| `deployment` | One CD deployment record (deployable job + environment) | `--with-deployments` or `--full` |
| `environment` | A deploy target (development/staging/production/etc.), with its last deployment | `--with-environments` or `--full` |

Column reuse per row type:

| Column | `pipeline_run` | `ci_config` | `reusable_template` | `deployment` | `environment` |
|---|---|---|---|---|---|
| `pipeline_id` | pipeline ID | — | — | deployment iid | env id |
| `branch` | ref | branch checked | branch scanned | deployed ref | last-deploy ref |
| `status` | run status | `found` / `external` | — | deploy status | env state |
| `source` | pipeline source | — | — | deployable job name | env tier |
| `sha` | commit SHA | — | — | deployed SHA | last-deploy SHA |
| `file_path` | — | CI file path | template file path | — | — |
| `web_url` | run URL | file URL | file URL | deployment URL | env external URL |
| `environment` | — | — | — | env name | env name |

Other column notes:

- `group` — the root group you queried (inferred when using
  `--projects-csv` if all projects share a common top-level segment).
- `subgroup` — the full nested path between the root and the project
  (empty when the project sits directly under the root).
- `project` — the repo name.

> **CI vs CD pipelines.** GitLab doesn't tag pipelines as "CI" or "CD" in
> the API. Use the `source` column on `pipeline_run` rows together with
> `deployment` / `environment` rows for the CD side. A pipeline that
> produced a deployment will have the same `sha` as a `deployment` row.

> **Row order**: rows are written in the order projects *finish*, not the
> order they were listed.

## All CLI flags

```text
python fetch_gitlab_pipelines.py --help
```

### Project source (exactly one required)

| Flag | Notes |
|---|---|
| `--group` | Group full path or numeric ID. Recurses all subgroups. |
| `--projects-csv` | Reuse `gitlab-stats.csv` from the repo inventory. Skips the group-listing call. `ci_config_path` (rare custom paths) falls back to `.gitlab-ci.yml`. |

### Authentication

| Flag | Default | Notes |
|---|---|---|
| `--gitlab-url` | env `GITLAB_URL` or `https://gitlab.com` | |
| `--token` | env `GITLAB_TOKEN` | Single PAT |
| `--tokens` | env `GITLAB_TOKENS` | Comma-separated PATs |
| `--tokens-file` | — | One PAT per line (`#` comments) |

### Performance

| Flag | Default | Notes |
|---|---|---|
| `--workers-per-token` | `4` | Concurrent worker threads per token |
| `--rate-limit-floor` | `50` | Pause a token when `RateLimit-Remaining` drops below this |

### Output / resume

| Flag | Default | Notes |
|---|---|---|
| `--output` | `./output/pipelines.csv` | |
| `--checkpoint` | `./output/.processed_pipelines` | Append-only list of completed project IDs |
| `--no-resume` | off | Ignore checkpoint; reprocess everything |

### Scope filters (what to collect)

| Flag | Default | Notes |
|---|---|---|
| `--no-runs` | runs ON | Skip the per-project pipeline-run pagination (big speedup — pipeline runs dominate the API cost on active projects) |
| `--no-templates` | templates ON | Skip the `templates/` / `ci-templates/` / `ci/` tree scan |
| `--all-branches` | default branch only | Scan `ci_config` and `reusable_template` on EVERY branch of every project. Multiplies API calls by the average branch count. |
| `--with-deployments` | off | Add `deployment` rows (one per deployment record) |
| `--with-environments` | off | Add `environment` rows (one per deploy target) |
| `--full` | off | Shortcut for `--all-branches --with-deployments --with-environments` (pipeline runs stay on unless you also pass `--no-runs`) |

### Diagnostics

| Flag | Notes |
|---|---|
| `--verbose` / `-v` | DEBUG logging (retries, 429s, token rotation, etc.) |

## Summary output

The script prints periodic progress lines and a final summary:

```text
[ok 412/1000] my-org/team-a/repo  ci=found:3br templates=12 runs=87 deploys=24 envs=4 branches=42
[progress] 425/1000 done  (2.1 proj/s, elapsed 200s, eta ~273s)
...
[done] 24317 row(s) written to ./output/pipelines.csv
[stats] pipeline_run=17821 ci_config=2503 reusable_template=2940 deployment=910 environment=143
```

- `ci=found:3br` (with `--all-branches`) = the CI config was found on 3 branches.
- `branches=42` (only shown with `--all-branches`) = how many branches were scanned for ci_config and templates.

## Resume after Ctrl+C / crash

Just re-run with the same args. Completed project IDs are stored in
`output/.processed_pipelines` and skipped on the next run. The CSV is
opened in append mode and reuses the existing header.

To start from scratch:

```powershell
python fetch_gitlab_pipelines.py --group your-group --no-resume
# or delete output/.processed_pipelines and output/pipelines.csv first
```

## Performance notes

For 1000+ projects the dominant cost is `pipeline_run` pagination (every
run on every branch). The other big one is `--all-branches`, which
multiplies the cost of `ci_config` + `reusable_template` by the average
branches-per-project.

Levers:

1. `--no-runs` — drop the run-history rows; keep CI configs + templates.
   Rough speedup: 10–50× depending on how active your projects are.
2. Avoid `--all-branches` unless you specifically need branch-specific CI
   overrides. Most projects share one `.gitlab-ci.yml` across all branches.
3. `--workers-per-token N` with multiple PATs. Each PAT belonging to a
   distinct user contributes its own rate-limit budget.

Rough recipes:

```powershell
# Maximum coverage, accept long runtime (3 PATs from 3 different users)
$env:GITLAB_TOKENS = "glpat-a,glpat-b,glpat-c"
python fetch_gitlab_pipelines.py --group your-group --full --workers-per-token 4

# Maximum speed inventory (no run history, default branch only,
# 3 PATs, 6 workers each)
$env:GITLAB_TOKENS = "glpat-a,glpat-b,glpat-c"
python fetch_gitlab_pipelines.py --group your-group `
    --no-runs --workers-per-token 6
```

## Troubleshooting

### Constant 429s
Your tokens may share a user. Lower `--workers-per-token` (try `2`) or
raise `--rate-limit-floor` (try `200`) so the pool throttles itself
sooner.

### "All retries exhausted"
A specific project's API call kept failing (network, 5xx). That project
is logged as `[fail]` and skipped; the rest of the run continues. Re-run
with the checkpoint to retry only the failed ones (delete their IDs from
the checkpoint file first).

### `--projects-csv` says "Multiple root groups in CSV"
Your repo inventory contains projects from more than one root group. The
`group` column in the output CSV will be empty in that case. To get the
column populated, run the script with `--group <root>` instead.
