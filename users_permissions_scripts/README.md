# GitLab-to-GitHub Users & Permissions Migration

End-to-end tooling to **assess**, **map**, and **migrate** users and permissions from GitLab groups/projects to GitHub organizations/teams/repos.

## Overview

| Script | Purpose |
|--------|---------|
| `users_permissions_inventory.py` | Discovers GitLab groups, projects, users, and calculates effective permissions with inheritance. Outputs an Excel report + mapping CSV templates. |
| `migrate_permissions.py` | Reads completed mapping CSVs and executes the migration on GitHub — creates teams, adds members, sets repo permissions. |

## Prerequisites

- Python 3.10+
- GitLab PAT with `read_api` scope
- GitHub PAT with `admin:org`, `repo`, `read:user` scopes

```bash
pip install -r requirements.txt
```

## Workflow

### Step 1: Run Inventory (GitLab → Excel + CSVs)

```bash
python users_permissions_inventory.py --group <group-id-or-path> --token glpat-xxx
```

**Options:**
| Flag | Description | Default |
|------|-------------|---------|
| `--group`, `-g` | GitLab group ID or full path (required) | — |
| `--token`, `-t` | GitLab PAT (or set `GITLAB_TOKEN` env var) | — |
| `--url` | GitLab instance URL | `https://gitlab.com` |
| `--workers` | Concurrent API threads | `4` |
| `--page-size` | API pagination size | `100` |
| `--debug` | Enable debug logging | `false` |

**Outputs:**
- `GitLab_UsersPermissions_Inventory.xlsx` — 13-sheet Excel workbook
- `gitlab_to_github_user_mapping.csv`
- `gitlab_to_github_group_mapping.csv`
- `gitlab_to_github_project_mapping.csv`

**Excel Sheets:**
| Sheet | Contents |
|-------|----------|
| Summary | High-level stats |
| Groups | All groups/subgroups with depth |
| Projects | All projects with visibility, archive status |
| Users | User details (2FA, external, state) |
| GroupMemberships | Direct group role assignments |
| ProjectMemberships | Direct project role assignments |
| SharedGroupLinks | Groups shared with other groups |
| SharedProjectLinks | Projects shared with groups |
| EffectiveGroupPermissions | Calculated group perms with inheritance |
| EffectiveProjectPermissions | Calculated project perms with GitHub role mapping |
| SSHKeys | User SSH keys inventory |
| DeployKeys | Project deploy keys inventory |
| ApprovalRules | Project-level approval rules |

### Step 2: Fill in Mapping CSVs

Edit the 3 generated CSVs to map GitLab entities to GitHub targets:

**`gitlab_to_github_user_mapping.csv`** — fill `github_username`:
```csv
gitlab_username,github_username
john.doe,johndoe-gh
```

**`gitlab_to_github_group_mapping.csv`** — fill `github_org`, `github_team_slug`, `github_team_parent`:
```csv
gitlab_group_path,github_org,github_team_slug,github_team_parent
my-org/backend,my-github-org,backend,my-org
```

**`gitlab_to_github_project_mapping.csv`** — fill `github_org`, `github_repo_name`, `github_visibility`:
```csv
gitlab_project_path,github_org,github_repo_name,github_visibility
my-org/backend/api,my-github-org,api,private
```

### Step 3: Dry Run

```bash
python migrate_permissions.py \
  --github-org my-org \
  --github-token ghp_xxx \
  --mappings-dir . \
  --dry-run
```

### Step 4: Execute Migration

```bash
python migrate_permissions.py \
  --github-org my-org \
  --github-token ghp_xxx \
  --mappings-dir .
```

**Options:**
| Flag | Description |
|------|-------------|
| `--github-org` | Target GitHub organization (required) |
| `--github-token` | GitHub PAT (or set `GITHUB_TOKEN` env var) |
| `--mappings-dir` | Directory containing mapping CSVs + Excel |
| `--dry-run` | Simulate without making changes |
| `--skip-teams` | Skip team creation step |
| `--skip-members` | Skip member addition step |
| `--skip-repos` | Skip repository permission step |
| `--debug` | Enable debug logging |

### Step 5: Re-run (Idempotent)

The migration script is fully idempotent:

| State | Behavior |
|-------|----------|
| Already exists + matches | **SKIP** (no API call) |
| Already exists + mismatch | **UPDATE** (fix drift) |
| Doesn't exist | **CREATE** |

Safe to re-run anytime. Useful after adding new repos or changing roles in GitLab.

```bash
# Re-run only repo permissions after creating missing repos
python migrate_permissions.py \
  --github-org my-org \
  --github-token ghp_xxx \
  --mappings-dir . \
  --skip-teams --skip-members
```

## Permission Mapping

| GitLab Role | GitHub Team Role | GitHub Repo Permission |
|-------------|-----------------|----------------------|
| Guest / Reporter | member | pull (read) |
| Developer | member | push (write) |
| Maintainer | maintainer | maintain |
| Owner | maintainer | admin |

## Architecture

```
GitLab                          GitHub
──────                          ──────
Group (top-level)       →       Organization
├── Subgroup            →       Team (nested under parent team)
│   └── Project         →       Repository
└── Members             →       Team members + repo collaborators
    └── Inherited perms →       Team-level repo permissions
    └── Direct perms    →       Direct collaborator permissions
```

## File Structure

```
users_permissions_scripts/
├── README.md
├── requirements.txt
├── users_permissions_inventory.py    # Assessment/inventory tool
├── migrate_permissions.py            # Migration execution tool
├── gitlab_to_github_user_mapping.csv
├── gitlab_to_github_group_mapping.csv
└── gitlab_to_github_project_mapping.csv
```

## Logs

Both scripts write logs to the same directory:
- `users_permissions_inventory.log`
- `migrate_permissions.log`
