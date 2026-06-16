# GitLab-to-GitHub Package Migration Scripts

Tools for migrating packages from GitLab Package Registry to GitHub Packages, with SHA-256 integrity verification.

## Background

During GH-to-GH package migrations, SHAs were observed to change after migration. These scripts verify whether the same issue occurs in GitLab-to-GitHub migrations.

## Scripts

| Script | Purpose |
|--------|---------|
| `migrate_packages.py` | Downloads packages from GitLab, uploads to GitHub Packages |
| `verify_sha.py` | Compares SHA-256 checksums pre/post migration to detect integrity changes |

## Supported Package Types

- **npm** → GitHub npm registry (`npm.pkg.github.com`)
- **Maven** → GitHub Maven registry (`maven.pkg.github.com`)
- **NuGet** → GitHub NuGet registry (`nuget.pkg.github.com`)
- **Generic** → GitHub Releases (closest equivalent)
- **PyPI** → GitHub Releases (GitHub doesn't natively support PyPI)

## Requirements

```bash
pip install -r requirements.txt
```

- Python 3.10+
- GitLab PAT with `read_api` scope
- GitHub PAT with `write:packages`, `read:packages` scope

## Quick Start

### 1. Migrate Packages

```bash
# Dry run first (downloads + SHAs, no upload)
python migrate_packages.py \
  --gitlab-group my-org \
  --gitlab-token glpat-xxx \
  --github-org my-gh-org \
  --github-token ghp_xxx \
  --dry-run

# Live migration
python migrate_packages.py \
  --gitlab-group my-org \
  --gitlab-token glpat-xxx \
  --github-org my-gh-org \
  --github-token ghp_xxx
```

### 2. Verify SHA Integrity (Post-Migration)

```bash
# Using migration report
python verify_sha.py \
  --report /path/to/migration_report.json \
  --gitlab-token glpat-xxx \
  --github-token ghp_xxx \
  --github-org my-gh-org

# Full scan (compares all packages)
python verify_sha.py \
  --full-scan \
  --gitlab-group my-org \
  --gitlab-token glpat-xxx \
  --github-token ghp_xxx \
  --github-org my-gh-org

# Single package verification
python verify_sha.py \
  --package-name my-package \
  --package-version 1.0.0 \
  --gitlab-project-id 123 \
  --gitlab-token glpat-xxx \
  --github-token ghp_xxx \
  --github-org my-gh-org \
  --github-repo my-repo
```

## Environment Variables

| Variable | Description |
|----------|-------------|
| `GITLAB_URL` | GitLab instance URL (default: `https://gitlab.com`) |
| `GITLAB_TOKEN` | GitLab Personal Access Token |
| `GITHUB_TOKEN` | GitHub Personal Access Token |
| `GITHUB_ORG` | GitHub organization name |

## Workflow

```
┌─────────────────────────────────────────────────────────────────┐
│  1. migrate_packages.py --dry-run                               │
│     → Downloads all packages from GitLab                        │
│     → Computes SHA-256 for each file                            │
│     → Validates GitLab-reported SHA vs downloaded content        │
│     → Generates migration_report.json                           │
├─────────────────────────────────────────────────────────────────┤
│  2. migrate_packages.py (live)                                  │
│     → Downloads from GitLab + uploads to GitHub                 │
│     → Records all SHAs in migration_report.json                 │
├─────────────────────────────────────────────────────────────────┤
│  3. verify_sha.py --report migration_report.json                │
│     → Downloads packages from GitHub                            │
│     → Compares GitHub SHA vs original GitLab SHA                │
│     → Reports any mismatches (like the GH-GH issue)            │
│     → Generates sha_verification_report.json                    │
└─────────────────────────────────────────────────────────────────┘
```

## Output Reports

### migration_report.json
```json
{
  "packages_found": 15,
  "packages_migrated": 12,
  "packages_skipped": 2,
  "packages_failed": 1,
  "sha_mismatches": [],
  "details": [...]
}
```

### sha_verification_report.json
```json
{
  "total_files_checked": 30,
  "sha_matches": 28,
  "sha_mismatches": 2,
  "mismatches": [
    {
      "file": "artifact-1.0.jar",
      "package": "my-lib@1.0.0",
      "gitlab_sha256": "abc123...",
      "github_sha256": "def456..."
    }
  ]
}
```

## Troubleshooting

| Issue | Fix |
|-------|-----|
| 403 on GitLab | Ensure token has `read_api` scope |
| 403 on GitHub | Ensure token has `write:packages` + `read:packages` |
| 409 Conflict on GitHub | Package version already exists (safe to ignore) |
| SHA mismatch on download | GitLab may report stale SHAs; the computed SHA is authoritative |
| Generic packages missing | GitHub doesn't have a generic registry; they're stored as release assets |
