# GitLab Inventory Scripts

A collection of Python scripts to inventory GitLab groups — capturing repository metadata and pipeline assets into CSV files for migration planning and analysis.

## Structure

```
├── repo_inventory_scripts/     # Project-level stats (size, commits, branches, CI, etc.)
│   └── gitlab.py
├── pipeline_inventory_scripts/ # Pipeline runs, CI configs, and reusable templates
│   └── fetch_gitlab_pipelines.py
```

## Prerequisites

- Python 3.7+
- A GitLab Personal Access Token with `read_api` and `read_repository` scopes

## Quick Start

### 1. Install dependencies

```bash
pip install requests
```

### 2. Set your token

**PowerShell:**
```powershell
$env:GITLAB_TOKEN = "glpat-xxxxxxxxxxxx"
```

**Bash:**
```bash
export GITLAB_TOKEN="glpat-xxxxxxxxxxxx"
```

### 3. Run the repository inventory

```bash
cd repo_inventory_scripts
# Set the group to scan
$env:GITLAB_GROUP = "your-group"
python gitlab.py
```

Output: `repo_inventory_scripts/data/gitlab-stats.csv`

### 4. Run the pipeline inventory

```bash
cd pipeline_inventory_scripts
python fetch_gitlab_pipelines.py --group your-group
```

Output: `pipeline_inventory_scripts/output/pipelines.csv`

## Scripts

### Repository Inventory (`repo_inventory_scripts/gitlab.py`)

Fetches comprehensive statistics for every project in a GitLab group (including subgroups):

- Repository size, commit count, branch count, file count
- Large file detection (>100 MB)
- CI/CD configuration detection
- Exportable model counts (merge requests, issues, webhooks, tags, milestones, wikis)
- Size threshold flags for migration planning (>2 GB, >6 GB)

See [repo_inventory_scripts/README.md](repo_inventory_scripts/README.md) for full details.

### Pipeline Inventory (`pipeline_inventory_scripts/fetch_gitlab_pipelines.py`)

Walks a GitLab group recursively and produces a single CSV with:

| Row type | Description |
|---|---|
| `pipeline_run` | Every actual pipeline execution (all branches/tags/statuses) |
| `ci_config` | Each project's `.gitlab-ci.yml` or custom CI config path |
| `reusable_template` | YAML files in `templates/`, `ci-templates/`, or `ci/` directories |

See [pipeline_inventory_scripts/README.md](pipeline_inventory_scripts/README.md) for full details.

## Configuration

Both scripts support self-managed GitLab instances:

```bash
# Environment variable (repo inventory)
$env:GITLAB_URL = "https://gitlab.example.com"

# CLI flag (pipeline inventory)
python fetch_gitlab_pipelines.py --group my-org --gitlab-url https://gitlab.example.com
```

## License

Internal use — Hexaware Technologies.