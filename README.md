# GitLab Inventory Scripts

Two Python scripts that walk a GitLab group (recursing through every nested
subgroup, any depth) and dump per-project inventory data into CSV files —
built for migration planning at scale. Both scripts run in parallel across
multiple PATs with rate-limit-aware throttling and crash-safe resume.

```
gitlab_inventory_scripts/
├── .env-example                # copy to .env, fill in your values
├── .gitignore
├── requirements.txt            # shared deps for both scripts
├── gitlab_client.py            # shared: token pool, pooled HTTP client,
│                               # .env autoload, checkpoint, CSV sink
├── repo_inventory_scripts/
│   ├── gitlab.py               # per-project stats -> data/gitlab-stats.csv
│   └── README.md
└── pipeline_inventory_scripts/
    ├── fetch_gitlab_pipelines.py   # CI configs + pipeline runs + reusable
    │                               # YAML + deployments + environments
    │                               # -> output/pipelines.csv
    └── README.md
```

For full flags, output schemas, performance notes, and troubleshooting, see
each script's own README:

- **Repo inventory** — [repo_inventory_scripts/README.md](repo_inventory_scripts/README.md)
- **Pipeline inventory** — [pipeline_inventory_scripts/README.md](pipeline_inventory_scripts/README.md)

## Quick start

### 1. Install

Python 3.10+. From this workspace root:

```powershell
pip install -r requirements.txt
```

### 2. Configure with a `.env` file

```powershell
Copy-Item .env-example .env
notepad .env       # fill in your token + group
```

`.env` (single token):

```bash
GITLAB_TOKEN=glpat-xxxxxxxxxxxx
GITLAB_GROUP=your-group
# GITLAB_URL=https://gitlab.example.com    # self-managed
```

`.env` (token pool — for parallel throughput):

```bash
GITLAB_TOKENS=glpat-a,glpat-b,glpat-c
GITLAB_GROUP=your-group
```

> **Multi-PAT throughput note.** GitLab applies its primary rate limit
> **per authenticated user**, not per token. To actually get Nx throughput
> each PAT must belong to a distinct user (or service account).

Shell-set env vars (`$env:GITLAB_TOKEN = "..."`) still work and override
`.env`.

### 3. Run the repo inventory

```powershell
cd repo_inventory_scripts
python gitlab.py                  # full data; slow for big groups
python gitlab.py --no-branch-walk # ~10x faster; skips per-branch walks
```

Output → `repo_inventory_scripts/data/gitlab-stats.csv`.
Full options → [repo_inventory_scripts/README.md](repo_inventory_scripts/README.md).

### 4. Run the pipeline inventory

```powershell
cd ..\pipeline_inventory_scripts

# Full picture: pipeline runs + CI configs + templates on every branch +
# deployments + environments
python fetch_gitlab_pipelines.py --group your-group --full

# Or reuse the project list from the repo inventory (skips the group walk)
python fetch_gitlab_pipelines.py `
    --projects-csv ..\repo_inventory_scripts\data\gitlab-stats.csv --full
```

Output → `pipeline_inventory_scripts/output/pipelines.csv`.
Full options → [pipeline_inventory_scripts/README.md](pipeline_inventory_scripts/README.md).

### 5. Resume after Ctrl+C / crash

Just re-run the same command. Both scripts maintain a checkpoint of
completed project IDs and skip them on the next run.


