# Package and Container Migration Guide

## Purpose

This document explains how to run and validate migration from GitLab to GitHub for:
- Package Registry artifacts
- Container Registry images
- SHA and digest integrity checks

It covers dry-run, live migration, verification, output reports, and troubleshooting.

## Scripts Covered

- migrate_packages.py
  - Migrates package artifacts from GitLab Package Registry to GitHub package targets
- migrate_containers.py
  - Migrates container images and tags from GitLab Container Registry to GHCR
- verify_sha.py
  - Compares post-migration SHA values against source metadata

## Package Type Behavior

Current package migration supports these package types:

- npm
  - Upload path: GitHub npm registry at https://npm.pkg.github.com
  - Uses npm publish (CLI) with temporary npm auth configuration
  - Source npm scope is normalized to the target GitHub org scope during publish
- maven
  - Upload path: GitHub Maven registry at https://maven.pkg.github.com
  - Uses HTTP upload with retry logic
- nuget
  - Upload path: GitHub NuGet registry at https://nuget.pkg.github.com
  - Uses dotnet nuget push with skip-duplicate behavior
- generic
  - Upload path: GitHub Release assets in the mapped repository
- pypi
  - Upload path: GitHub Release assets (same generic flow)

## Requirements

## System and Tools

- Python 3.10+
- pip dependencies from package_migration_scripts/requirements.txt
- npm CLI (for npm package publishing)
- dotnet CLI (for NuGet package publishing)
- For container migration:
  - skopeo preferred, or
  - crane, docker, or podman

## Access and Tokens

- GitLab PAT with:
  - read_api for package discovery and downloads
  - read_registry for container migration
- GitHub PAT with:
  - write:packages
  - read:packages
  - repo (recommended for repository creation and release asset fallback)

## Install

From repository root:

```powershell
pip install -r package_migration_scripts/requirements.txt
```

## Environment Variables

You can pass values by CLI flags or env vars.

- GITLAB_URL (default: https://gitlab.com)
- GITLAB_TOKEN
- GITHUB_TOKEN
- GITHUB_ORG

Example PowerShell setup:

```powershell
$env:GITLAB_TOKEN = "glpat-xxxx"
$env:GITHUB_TOKEN = "ghp_xxxx"
$env:GITHUB_ORG = "your-github-org"
```

## Package Migration Runbook

## 1) Dry-run first

Dry-run downloads packages, computes SHA, and validates flow without publishing.

```powershell
python package_migration_scripts/migrate_packages.py \
  --gitlab-group your-gitlab-group \
  --dry-run
```

## 2) Live package migration

```powershell
python package_migration_scripts/migrate_packages.py \
  --gitlab-group your-gitlab-group
```

## 3) Verify SHA integrity

Using the migration report:

```powershell
python package_migration_scripts/verify_sha.py \
  --report <path-to-migration_report.json> \
  --gitlab-token $env:GITLAB_TOKEN \
  --github-token $env:GITHUB_TOKEN \
  --github-org $env:GITHUB_ORG
```

Optional full scan mode:

```powershell
python package_migration_scripts/verify_sha.py \
  --full-scan \
  --gitlab-group your-gitlab-group \
  --gitlab-token $env:GITLAB_TOKEN \
  --github-token $env:GITHUB_TOKEN \
  --github-org $env:GITHUB_ORG
```

## Container Migration Runbook

## 1) Dry-run container migration

```powershell
python package_migration_scripts/migrate_containers.py \
  --gitlab-group your-gitlab-group \
  --gitlab-token $env:GITLAB_TOKEN \
  --github-org $env:GITHUB_ORG \
  --github-token $env:GITHUB_TOKEN \
  --dry-run
```

## 2) Live container migration

```powershell
python package_migration_scripts/migrate_containers.py \
  --gitlab-group your-gitlab-group \
  --gitlab-token $env:GITLAB_TOKEN \
  --github-org $env:GITHUB_ORG \
  --github-token $env:GITHUB_TOKEN
```

## 3) Live container migration with digest verification

```powershell
python package_migration_scripts/migrate_containers.py \
  --gitlab-group your-gitlab-group \
  --gitlab-token $env:GITLAB_TOKEN \
  --github-org $env:GITHUB_ORG \
  --github-token $env:GITHUB_TOKEN \
  --verify
```

## Reports and Outputs

migrate_packages.py generates:
- migration_report.json in the working directory

Report includes:
- packages_found
- packages_migrated
- packages_skipped
- packages_failed
- sha_mismatches
- errors
- details per package version and file

verify_sha.py generates:
- sha_verification_report.json

migrate_containers.py generates:
- container migration report with copied images, digest checks, failures

## Important Operational Notes

- Repository existence
  - Package migration ensures target GitHub repositories exist and attempts creation when missing.
- Existing versions
  - 409 conflicts are treated as already exists and not hard failures.
- npm scope
  - Source npm package names are rewritten to the target GitHub org scope before publish.
- NuGet duplicates
  - NuGet upload uses skip-duplicate semantics.
- Generic and PyPI behavior
  - Both use GitHub Releases asset upload path.

## Troubleshooting

## Auth errors

- GitLab 401/403
  - Validate PAT scope and token value
- GitHub 401/403
  - Confirm write:packages, read:packages, and repo permissions

## npm publish failures

- Ensure npm CLI exists and is callable
- Ensure package name is valid for target org scope
- Ensure token has permission for target org package publish

## NuGet push failures

- Ensure dotnet CLI exists
- Ensure token has package publish permissions
- Verify target org and source URL are correct

## Release-asset fallback failures

- Ensure target repo exists and is accessible
- Ensure PAT includes repo scope for release operations

## Best Practice Execution Order

1. Run package dry-run
2. Run container dry-run
3. Run package live migration
4. Run container live migration
5. Run SHA and digest verification
6. Review reports and rerun only failed units

## Recommended Post-Migration Checks

- Confirm package versions appear in GitHub org package views
- Confirm container images and tags appear in GHCR
- Validate install/pull from target endpoints:
  - npm install
  - dotnet restore
  - maven dependency resolution
  - docker pull or crane digest checks
- Archive reports for audit trail
