# GitLab Branch Protection Creator

Creates branch protection rules for all projects in a GitLab group using the GitLab REST API.

## Features

- Apply consistent branch protection rules across all projects in a group
- Configurable rules via YAML (branch patterns, access levels, force push, etc.)
- Dry-run mode to preview changes before applying
- Skips already-protected branches (configurable)
- Skips branches that don't exist in a project
- Wildcard branch patterns (e.g., `release-*`)
- Parallel execution with configurable workers
- Token pool support for rate-limit management
- JSON + CSV reports of all actions taken

## Prerequisites

- Python 3.9+
- GitLab PAT with `api` scope (needs Maintainer+ access on target projects)

## Setup

```bash
pip install -r requirements.txt
```

Set your GitLab token:
```bash
export GITLAB_TOKEN=glpat-xxxxxxxxxxxxxxxxxxxx
# Or for multiple tokens:
export GITLAB_TOKENS=glpat-token1,glpat-token2
```

## Usage

### Basic (protect main/master with defaults)

```bash
python create_branch_protection.py --group my-org/my-team
```

### Dry-run (preview without making changes)

```bash
python create_branch_protection.py --group my-org/my-team --dry-run
```

### Custom config

```bash
python create_branch_protection.py --group 12345 --config my_rules.yaml
```

### With token file

```bash
python create_branch_protection.py --group my-org --tokens-file tokens.txt
```

### Include archived projects

```bash
python create_branch_protection.py --group my-org --include-archived
```

## Configuration (config.yaml)

```yaml
rules:
  - name: "main"
    push_access_level: 40        # 0=No one, 30=Developer, 40=Maintainer
    merge_access_level: 40
    unprotect_access_level: 40
    allow_force_push: false
    code_owner_approval_required: false  # Premium/Ultimate only

  - name: "release-*"
    push_access_level: 0         # No direct pushes
    merge_access_level: 40
    allow_force_push: false

options:
  skip_existing: true
  protect_missing_branches: false
  dry_run: false
  workers: 4
```

### Access Levels

| Level | Description |
|-------|-------------|
| 0     | No access (no one can push directly) |
| 30    | Developer |
| 40    | Maintainer |
| 60    | Admin (self-managed only) |

## CLI Options

| Flag | Description |
|------|-------------|
| `--group` | GitLab group ID or full path (required) |
| `--config` | Path to YAML config (default: `config.yaml`) |
| `--gitlab-url` | GitLab instance URL (default: `https://gitlab.com`) |
| `--tokens` | Comma-separated PATs |
| `--tokens-file` | File with one token per line |
| `--dry-run` | Preview mode, no changes |
| `--output-dir` | Report output directory |
| `--workers` | Parallel workers (overrides config) |
| `--include-archived` | Include archived projects |
| `-v, --verbose` | Debug logging |

## Output

Reports are written to `reports/` (or `--output-dir`):
- `branch_protection_report_<timestamp>.json` — full details
- `branch_protection_report_<timestamp>.csv` — summary table

## Examples

### Protect only main, allow developers to merge

```yaml
rules:
  - name: "main"
    push_access_level: 40
    merge_access_level: 30
    allow_force_push: false
```

### Lock down release branches completely

```yaml
rules:
  - name: "release-*"
    push_access_level: 0
    merge_access_level: 40
    allow_force_push: false
    code_owner_approval_required: true
```
