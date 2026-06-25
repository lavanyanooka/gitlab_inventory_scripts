# GitLab-to-GitHub Branch Protection Migration

Production-ready tool to migrate GitLab branch protection rules to GitHub branch protection rules.

## Folder Structure

```
branch_protection_scripts/
├── config/
│   ├── config_manager.py        # Configuration loading & merging
│   └── default_config.yaml      # Default configuration values
├── samples/
│   ├── repo_mapping.csv         # Sample repo mapping (CSV)
│   ├── repo_mapping.json        # Sample repo mapping (JSON)
│   └── sample_config.yaml       # Sample user configuration
├── tests/
│   ├── test_mapping_engine.py   # Mapping engine unit tests
│   └── test_validation_engine.py # Validation engine unit tests
├── logs/                         # Generated log files
├── reports/                      # Generated reports
├── cli.py                        # CLI entry point
├── gitlab_client.py              # GitLab REST API client
├── github_client.py              # GitHub REST API client
├── mapping_engine.py             # GitLab → GitHub rule mapping
├── migration_engine.py           # Migration orchestration
├── validation_engine.py          # Post-migration validation
├── report_generator.py           # Report generation (JSON/CSV/MD)
├── requirements.txt              # Python dependencies
└── README.md
```

## Requirements

```bash
pip install -r requirements.txt
```

- Python 3.10+
- GitLab PAT with `read_api` scope
- GitHub PAT with `repo`, `admin:org` scopes

## Environment Variables

| Variable | Description |
|----------|-------------|
| `GITLAB_URL` | GitLab instance URL (default: `https://gitlab.com`) |
| `GITLAB_TOKEN` | GitLab Personal Access Token |
| `GITHUB_TOKEN` | GitHub Personal Access Token |
| `GITHUB_ORG` | Target GitHub organization |

## Quick Start

### 1. Prepare Repository Mapping

Create a CSV or JSON file mapping GitLab projects to GitHub repos:

**CSV format:**
```csv
gitlab_project_id,github_owner,github_repo
12345678,my-org,web-frontend
12345679,my-org,api-service
```

**JSON format:**
```json
[
  {"gitlab_project_id": 12345678, "github_owner": "my-org", "github_repo": "web-frontend"},
  {"gitlab_project_id": 12345679, "github_owner": "my-org", "github_repo": "api-service"}
]
```

### 2. Dry Run (Preview Changes)

```bash
python -m branch_protection_scripts.cli \
  --repo-mapping samples/repo_mapping.csv \
  --gitlab-token $GITLAB_TOKEN \
  --github-token $GITHUB_TOKEN \
  --github-org my-org \
  --dry-run
```

### 3. Live Migration

```bash
python -m branch_protection_scripts.cli \
  --repo-mapping samples/repo_mapping.csv \
  --gitlab-token $GITLAB_TOKEN \
  --github-token $GITHUB_TOKEN \
  --github-org my-org
```

### 4. Validate Only (Check Existing Protections)

```bash
python -m branch_protection_scripts.cli \
  --repo-mapping samples/repo_mapping.csv \
  --github-token $GITHUB_TOKEN \
  --github-org my-org \
  --validate-only
```

### 5. Resume After Interruption

```bash
python -m branch_protection_scripts.cli \
  --repo-mapping samples/repo_mapping.csv \
  --gitlab-token $GITLAB_TOKEN \
  --github-token $GITHUB_TOKEN \
  --github-org my-org \
  --resume
```

## CLI Options

| Option | Description |
|--------|-------------|
| `--config`, `-c` | Path to YAML config file |
| `--repo-mapping`, `-m` | Path to repository mapping (CSV/JSON) — **required** |
| `--gitlab-url` | GitLab instance URL |
| `--gitlab-token` | GitLab PAT |
| `--github-token` | GitHub PAT |
| `--github-org` | Target GitHub organization |
| `--dry-run` | Preview changes without applying |
| `--validate-only` | Only validate, don't migrate |
| `--migrate-only` | Migrate without post-validation |
| `--resume` | Resume from last successful repo |
| `--skip-existing` | Skip branches with existing protection |
| `--include-repos` | Regex patterns for repos to include |
| `--exclude-repos` | Regex patterns for repos to exclude |
| `--include-branches` | Regex patterns for branches to include |
| `--exclude-branches` | Regex patterns for branches to exclude |
| `--workers` | Number of parallel workers |
| `--batch-size` | Batch size for processing |
| `--output-dir` | Report output directory |
| `--verbose`, `-v` | Enable DEBUG logging |

## Configuration

Use a YAML config file for persistent settings (see `samples/sample_config.yaml`):

```bash
python -m branch_protection_scripts.cli \
  --config my_config.yaml \
  --repo-mapping mapping.csv
```

**Priority:** CLI args > Environment variables > Config file > Defaults

## Branch Protection Mapping

| GitLab Setting | GitHub Equivalent |
|---------------|-------------------|
| Protected Branch name | Branch protection rule |
| Push Access Level (Maintainer) | Push restrictions |
| Merge Access Level | Required PR reviews |
| Allow Force Push | allow_force_pushes |
| Code Owner Approval | require_code_owner_reviews |
| Approval Rules (count) | required_approving_review_count |
| — | required_status_checks (configurable) |
| — | enforce_admins (configurable) |
| — | require_linear_history (configurable) |
| — | require_signed_commits (configurable) |
| — | allow_deletions (configurable) |

## Workflow

```
┌─────────────────────────────────────────────────────────────────┐
│  1. Load repo mapping & configuration                           │
│     → Validate required fields                                  │
│     → Apply filters (include/exclude repos/branches)            │
├─────────────────────────────────────────────────────────────────┤
│  2. For each repository:                                        │
│     → Verify GitLab project exists                              │
│     → Verify GitHub repository exists                           │
│     → Fetch GitLab protected branches                           │
│     → Fetch approval rules                                      │
├─────────────────────────────────────────────────────────────────┤
│  3. For each protected branch:                                  │
│     → Verify branch exists on GitHub                            │
│     → Map GitLab settings → GitHub API payload                  │
│     → Apply branch protection (or dry-run)                      │
│     → Retry on transient failures                               │
├─────────────────────────────────────────────────────────────────┤
│  4. Post-migration validation:                                  │
│     → Verify protection applied                                 │
│     → Compare expected vs actual settings                       │
│     → Report mismatches                                         │
├─────────────────────────────────────────────────────────────────┤
│  5. Generate reports (JSON, CSV, Markdown):                     │
│     → Migration summary                                         │
│     → Validation results                                        │
│     → Failed/skipped repositories                               │
└─────────────────────────────────────────────────────────────────┘
```

## Reports

Reports are generated in `reports/` (configurable) in three formats:

- **JSON** — Machine-readable, includes full details
- **CSV** — Spreadsheet-compatible summary
- **Markdown** — Human-readable with tables

### Migration Report Includes:
- Total/success/failed/skipped counts
- Per-branch status, duration, and error messages
- Failed repository details

### Validation Report Includes:
- Pass/fail per branch
- Per-check comparison (expected vs actual)
- Failed check details

## Error Handling

- **Rate limiting:** Automatic wait based on API headers
- **Transient errors (5xx):** Exponential backoff retry (configurable)
- **Repo/branch not found:** Logged and skipped, migration continues
- **Resume support:** State saved after each repo; use `--resume` to continue

## Running Tests

```bash
pip install pytest
pytest branch_protection_scripts/tests/ -v
```

## Notes

- Migration is **idempotent** — re-running applies the same protection
- Use `--skip-existing` to avoid overwriting manually-configured protections
- GitHub wildcard branch protection patterns (e.g., `release/*`) require a GitHub Pro/Enterprise plan
- Maximum 6 required reviewers on GitHub (GitLab values are capped automatically)
- State file saved at `logs/.migration_state.json` for resume support
