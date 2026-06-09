# Repository Inventory (`gitlab.py`)

Per-project inventory for every project under a GitLab group (recursing
through all nested subgroups). Writes one row per project to
`data/gitlab-stats.csv`.

Parallel, multi-token, rate-limit-aware, resume-safe.

## Install

Install the shared dependencies once from the workspace root:

```powershell
pip install -r ..\requirements.txt
```

Python 3.10+.

## Quick start

The recommended way is a `.env` file at the workspace root (copy
`../.env-example` and fill in your values). Both scripts auto-load it.

```powershell
# 1. From the workspace root:
Copy-Item ..\.env-example ..\.env
notepad ..\.env       # set GITLAB_TOKEN and GITLAB_GROUP

# 2. Then just:
python gitlab.py
```

Shell environment variables still work and override anything in `.env`:

```powershell
# Single token, no .env
$env:GITLAB_TOKEN = "glpat-xxxxxxxxxxxx"
$env:GITLAB_GROUP = "your-group"
python gitlab.py

# Multiple tokens (round-robin pool; each PAT should belong to a different
# user for real Nx throughput — see top-level README)
$env:GITLAB_TOKENS = "glpat-a,glpat-b,glpat-c"
python gitlab.py --workers-per-token 4

# Faster mode (skips per-branch commit/file walks)
python gitlab.py --no-branch-walk

# Self-managed GitLab
$env:GITLAB_URL = "https://gitlab.example.com"
python gitlab.py
```

## Output

`data/gitlab-stats.csv`, one row per project. Columns:

| Group | Columns |
|---|---|
| Identity | `id`, `name`, `parent_group`, `subgroups`, `subgroup_count`, `path`, `visibility`, `created_at`, `default_branch`, `web_url` |
| Activity | `status`, `archived`, `stars`, `forks`, `open_issues`, `last_activity`, `contributors`, `pr_count`, `total_commits` |
| Repo size | `branch_count`, `file_count`, `all_branches_file_count`, `total_objects`, `repository_size_mb`, `repository_size_gb`, `total_size_mb`, `total_size_gb` |
| Size flags | `has_large_file_100mb`, `exceeds_2gb`, `exceeds_6gb` |
| Features | `pipeline`, `has_lfs`, `lfs_file_count`, `lfs_total_size_bytes`, `lfs_total_size_mb`, `has_gitmodules`, `has_codeowners`, `has_pr_template`, `releases_count`, `branch_protections`, `has_rulesets`, `ruleset_count` |
| Migration counts | `exportable_users`, `exportable_protected_branches`, `exportable_merge_requests`, `exportable_mr_notes`, `exportable_issues`, `exportable_issue_notes`, `exportable_webhooks`, `exportable_tags`, `exportable_commit_comments`, `exportable_has_wiki`, `exportable_milestones` |

> **Row order**: rows are written in the order projects *finish*, not the
> order they were listed. Sort by `id` or `path` if you need a stable
> order.

## All CLI flags

```text
python gitlab.py --help
```

### Project source

| Flag | Default | Notes |
|---|---|---|
| `--group` | env `GITLAB_GROUP` / config | Group full path or numeric ID. Required (via flag, env, or config). |

### Authentication

| Flag | Default | Notes |
|---|---|---|
| `--gitlab-url` | env `GITLAB_URL` or `https://gitlab.com` | |
| `--token` | env `GITLAB_TOKEN` | Single PAT |
| `--tokens` | env `GITLAB_TOKENS` | Comma-separated PATs for round-robin pool |
| `--tokens-file` | — | One PAT per line (`#` comments) |

### Performance

| Flag | Default | Notes |
|---|---|---|
| `--workers-per-token` | `4` | Concurrent worker threads per token |
| `--rate-limit-floor` | `50` | Pause a token when `RateLimit-Remaining` drops below this |
| `--no-branch-walk` | off | Skip per-branch commit + file walks (much faster; uses API-reported `commit_count` from default branch only and loses cross-branch file dedup) |

### Output / resume

| Flag | Default | Notes |
|---|---|---|
| `--output` | `data/gitlab-stats.csv` | |
| `--checkpoint` | `data/.processed_projects` | Append-only list of completed project IDs |
| `--no-resume` | off | Ignore checkpoint; reprocess everything |
| `--verbose` / `-v` | off | DEBUG logging (retries, 429s, etc.) |

## Configuration sources (priority order)

1. CLI args (`--group`, `--tokens`, `--gitlab-url`, …)
2. Environment (`GITLAB_TOKEN`, `GITLAB_TOKENS`, `GITLAB_GROUP`, `GITLAB_URL`)
   — includes values auto-loaded from a `.env` file in the current
   working directory (or any parent) or next to `gitlab_client.py` at
   the workspace root.
3. `gl-migrate.conf` (JSON, or `key=value` lines)
4. `.token` (JSON)

> Shell-set environment variables ALWAYS win over `.env` (the autoload
> uses `override=False`).

Example `gl-migrate.conf`:

```ini
# Tokens — single
GITLAB_TOKEN=glpat-xxxxxxxxxxxx
# (or use GITLAB_TOKENS=a,b,c at the env level for multiple)

GITLAB_GROUP=engineering
GITLAB_URL=https://gitlab.example.com

# Optional: pre-filter projects via an existing CSV
project_list_file=projects-to-migrate.csv
migrate_repo_values=["Migrate","Yes"]
```

Example `.token` (JSON):

```json
{
  "token": "glpat-xxxxxxxxxxxx",
  "group": "engineering",
  "gitlab_url": "https://gitlab.example.com"
}
```

## Resume after Ctrl+C / crash

Just re-run with the same args. Completed project IDs are stored in
`data/.processed_projects` and skipped on the next run. The CSV is opened
in append mode and reuses the existing header.

To start from scratch:

```powershell
python gitlab.py --no-resume
# or delete data/.processed_projects and data/gitlab-stats.csv first
```

## Performance notes

For 1000+ projects the dominant cost is the **per-branch commit / file
walks**. Order-of-magnitude estimates:

| Mode | Per project (typical repo) | 1000 projects, 1 token, 4 workers | 1000 projects, 3 tokens × 4 workers |
|---|---|---|---|
| Default (`get_all_branches_*` ON) | 30 s – many minutes | hours to a full day | hours |
| `--no-branch-walk` | ~5–15 s | tens of minutes | minutes |

If you don't strictly need cross-branch commit/file counts for migration
sizing, **start with `--no-branch-walk`**.

## Project pre-filtering (optional)

To inventory only a subset of projects, set `project_list_file` in the
config and create a CSV in `data/` with these columns:

| Column | |
|---|---|
| `Name` | The project name as it appears in GitLab |
| `Migrate Repo` | Filter value (defaults match `MIGRATE_REPO_VALUES`, default `["Migrate"]`) |

> ⚠ Do **not** point `project_list_file` at the output `gitlab-stats.csv` —
> the script writes to that file and would overwrite the input.

## Troubleshooting

### "No GitLab group defined"
Set `--group`, `GITLAB_GROUP` env var, or `group` in your config file.
For subgroups, pass the full path (`parent-group/subgroup`) — the script
URL-encodes it automatically.

### "No GitLab token(s)"
Provide `--token`, `--tokens`, `--tokens-file`, or set `GITLAB_TOKEN` /
`GITLAB_TOKENS` env vars (or put them in a `.env` file at the workspace
root).

### `.env` is found but the first variable is `None`
The file was saved with a UTF-8 BOM (byte-order mark). Re-save it as
plain UTF-8 *without* a BOM:

- **VS Code**: bottom-right encoding picker → "Save with Encoding" → "UTF-8".
- **Notepad++**: "Encoding" menu → "UTF-8" (not "UTF-8-BOM").
- **PowerShell 7+**: `Set-Content -Encoding utf8NoBOM .env ...`.
- **PowerShell 5**: avoid `Set-Content`; use VS Code or `notepad` to edit.

### Hitting 429s constantly
Your tokens are all on the same user, or your workers-per-token is too
aggressive for your instance. Try:

```powershell
python gitlab.py --workers-per-token 2 --rate-limit-floor 100
```

### Output CSV is locked (open in Excel)
The script falls back to a timestamped backup name automatically. Close
Excel before re-running for a clean output.

### Need a clean run after schema/code changes
Delete `data/.processed_projects` and `data/gitlab-stats.csv` first, or
pass `--no-resume` and overwrite the output (`--output other.csv`).

## Security

- Tokens are masked in logs (`glpat-xx...yyyy`).
- Use tokens with the **minimum** scopes: `read_api`, `read_repository`.
- Never commit `gl-migrate.conf` / `.token` to version control. Add them
  to `.gitignore`.
