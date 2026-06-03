# fetch_gitlab_pipelines.py

Walks a GitLab group recursively (all nested subgroups, any depth) and writes a **single CSV** containing every pipeline asset found across projects.

## Install

```powershell
pip install -r requirements.txt
```

## Run

```powershell
$env:GITLAB_TOKEN = "glpat-xxxxxxxxxxxx"

# Everything (runs + CI configs + reusable templates)
python fetch_gitlab_pipelines.py --group migration-github1

# Only inventory (no run history)
python fetch_gitlab_pipelines.py --group migration-github1 --no-runs

# Skip template scan if it's slow on large repos
python fetch_gitlab_pipelines.py --group migration-github1 --no-templates

# Custom output path
python fetch_gitlab_pipelines.py --group migration-github1 --output all.csv

# Self-managed GitLab
python fetch_gitlab_pipelines.py --group my-org --gitlab-url https://gitlab.example.com
```

Token needs the `read_api` scope (and `read_repository` for private repos).

## Output

A single CSV at `output/pipelines.csv` (or the `--output` path you provide), with columns:

```
type, pipeline_id, group, subgroup, project, branch, status, source,
sha, file_path, file_ref, created_at, updated_at, web_url,
project_web_url, project_id
```

The `type` column distinguishes three kinds of rows — filter on it in Excel to switch views:

| `type` | What the row represents |
|---|---|
| `pipeline_run` | An actual pipeline execution — any branch/tag, any status |
| `ci_config` | The project's `.gitlab-ci.yml` (or custom `ci_config_path`, including external `path@group/project:ref`) |
| `reusable_template` | A `*.yml` / `*.yaml` file under `templates/`, `ci-templates/`, or `ci/` (typical home of shared group/subgroup pipelines) |

Other column notes:
* `group` — the root group you queried.
* `subgroup` — full nested path between the root and the project (empty when the project sits directly under the root).
* `project` — repo name.
* `web_url` — direct link to the pipeline run, or to the YAML file on the default branch.

The script also prints a summary at the end, e.g.:

```
[done] 412 row(s) written to output\pipelines.csv
[stats] pipeline_run=378 ci_config=21 reusable_template=13
```
