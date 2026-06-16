# GitLab Inventory Tools

Standalone, single-file Python tools for GitLab inventory discovery. Each script is fully self-contained with no project dependencies -- just install `requests` and `openpyxl` and run.

## Requirements

- Python 3.11+
- GitLab Personal Access Token with `read_api` scope

## Installation

```bash
pip install requests openpyxl pandas
```

## Authentication (all tools)

All three tools accept credentials the same way:

| Source | Variable | Description |
|--------|----------|-------------|
| Environment | `GITLAB_URL` | GitLab instance URL (default: `https://gitlab.com`) |
| Environment | `GITLAB_TOKEN` | Personal Access Token with `read_api` scope |
| CLI | `--gitlab-url` | Overrides `GITLAB_URL` |
| CLI | `--token` | Overrides `GITLAB_TOKEN` |
| CLI | `--tokens` | Comma-separated tokens for round-robin rate-limit rotation |

---

## 1. inventory.py -- Groups, Projects & Runners

Discovers Groups, Subgroups, Projects, and Runners. Generates `GitLab_Inventory_Report.xlsx` with 8 sheets.

### Usage

```bash
python inventory.py --group my-org --token glpat-xxx --debug
python inventory.py --group my-org --tokens "t1,t2,t3" --output report.xlsx
```

### CLI Options

| Option | Default | Description |
|--------|---------|-------------|
| `--group` | *required* | GitLab group path |
| `--output` | `GitLab_Inventory_Report.xlsx` | Output file |
| `--max-workers` | `4` | Thread pool for parallel runner discovery |
| `--timeout` | `30` | API timeout (seconds) |
| `--retries` | `3` | Retry count on transient errors |
| `--debug` | off | Debug logging |
| `--log-file` | | Log to file |

### Output Sheets

| Sheet | Contents |
|-------|----------|
| Executive_Summary | Timestamp and key metrics |
| Groups | All groups (root + descendants) |
| Subgroups | Descendant groups only |
| Projects | All projects including subgroup projects |
| Runners | Deduplicated runners with full details |
| Project_Runner_Mapping | Which runners serve which projects |
| Tag_Analysis | Tag usage across runners and projects |
| Statistics | Summary counts and metrics |

---

## 2. secrets_inventory.py -- CI/CD Variables

Collects CI/CD variables from groups, subgroups, and projects with distinct scope tracking. Generates `GitLab_Secrets_Variables_Report.xlsx` with 6 sheets.

### Usage

```bash
python secrets_inventory.py --group my-org --token glpat-xxx --debug
python secrets_inventory.py --group my-org --tokens "t1,t2,t3" --output report.xlsx --include-values
```

### CLI Options

| Option | Default | Description |
|--------|---------|-------------|
| `--group` | *required* | GitLab group path |
| `--output` | `GitLab_Secrets_Variables_Report.xlsx` | Output file |
| `--include-values` | off | Export variable values (CAUTION: exposes secrets) |
| `--include-archived` | off | Include archived projects |
| `--no-subgroups` | off | Skip subgroup discovery |
| `--skip-group-variables` | off | Skip group/subgroup variable scanning |
| `--skip-project-variables` | off | Skip project variable scanning |
| `--timeout` | `30` | API timeout (seconds) |
| `--retries` | `3` | Retry count |
| `--debug` | off | Debug logging |
| `--log-file` | | Log to file |

### Output Sheets

| Sheet | Contents |
|-------|----------|
| Executive_Summary | Metrics and metadata |
| Group_Variables | Root group CI/CD variables |
| Subgroup_Variables | Descendant group CI/CD variables |
| Project_Variables | Project CI/CD variables |
| All_Variables | Combined view of all scopes |
| Summary | Per-scope statistics |

---

## 3. registry_inventory.py -- Packages, Containers & Artifacts

Discovers Package Registry, Container Registry, and CI/CD Artifacts across all projects. Generates `gitlab-registry-inventory.xlsx`.

### Usage

```bash
python registry_inventory.py --group my-org --token glpat-xxx --debug
python registry_inventory.py --group my-org --token glpat-xxx --summary
python registry_inventory.py --group my-org --tokens "t1,t2,t3" --output report.xlsx
```

### CLI Options

| Option | Default | Description |
|--------|---------|-------------|
| `--group` | *required* | GitLab group path |
| `--output` | `gitlab-registry-inventory.xlsx` | Output file |
| `--summary` | off | Aggregated summary sheets |
| `--skip-files` | off | Skip package file size lookups (faster) |
| `--skip-packages` | off | Skip package registry scanning |
| `--skip-containers` | off | Skip container registry scanning |
| `--skip-artifacts` | off | Skip CI/CD artifacts scanning |
| `--timeout` | `30` | API timeout (seconds) |
| `--retries` | `3` | Retry count |
| `--debug` | off | Debug logging |
| `--log-file` | | Log to file |

### Output Sheets

| Sheet | Contents |
|-------|----------|
| Package Registry | Package versions (or summary per package with `--summary`) |
| Container Registry | Container tags (or summary per repo with `--summary`) |
| Artifacts | CI/CD job artifacts (or summary per project with `--summary`) |

---

## Common Features

All three tools share:

- **Multi-token rotation**: `--tokens t1,t2,t3` for rate-limit avoidance
- **Retry with exponential backoff**: On HTTP 429, 502, 503, 504
- **Connection/timeout recovery**: Automatic retry on network failures
- **Frozen headers, auto-filter, bold headers** in Excel output
- **Dual logging**: Console + optional log file (`--log-file`)
- **Zero project dependencies**: Each script is a single standalone file

## License

Internal tools for GitLab-to-GitHub migration assessment.
