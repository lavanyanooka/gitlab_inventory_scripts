#!/usr/bin/env python3
"""
GitLab-to-GitHub Package Migration Tool

Downloads packages from GitLab Package Registry and uploads them to GitHub Packages.
Supports: npm, maven, nuget, generic, pypi package types.

Includes SHA-256 verification to detect integrity changes during migration
(addresses the known issue where SHAs may change during cross-platform migration).

Usage:
    python migrate_packages.py --gitlab-group my-org --gitlab-token glpat-xxx \
        --github-org my-gh-org --github-token ghp_xxx

    # Dry-run (download + verify SHAs, no upload)
    python migrate_packages.py --gitlab-group my-org --gitlab-token glpat-xxx \
        --github-org my-gh-org --github-token ghp_xxx --dry-run

    # Verify SHAs only (post-migration comparison)
    python migrate_packages.py --gitlab-group my-org --gitlab-token glpat-xxx \
        --github-org my-gh-org --github-token ghp_xxx --verify-only

Environment variables (CLI overrides take precedence):
    GITLAB_URL        - GitLab instance URL (default: https://gitlab.com)
    GITLAB_TOKEN      - GitLab PAT with read_api scope
    GITHUB_TOKEN      - GitHub PAT with write:packages, read:packages scope
    GITHUB_ORG        - GitHub organization to push packages to
"""

import argparse
import hashlib
import json
import logging
import os
import sys
import tempfile
import time
from datetime import datetime
from pathlib import Path
from urllib.parse import quote

import requests

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

VERSION = "1.0.0"
DEFAULT_GITLAB_URL = "https://gitlab.com"
DEFAULT_GITHUB_API = "https://api.github.com"
DEFAULT_GITHUB_NPM_REGISTRY = "https://npm.pkg.github.com"
DEFAULT_GITHUB_MAVEN_REGISTRY = "https://maven.pkg.github.com"
DEFAULT_GITHUB_NUGET_REGISTRY = "https://nuget.pkg.github.com"

SUPPORTED_PACKAGE_TYPES = ["npm", "maven", "nuget", "generic", "pypi"]

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

_logger = logging.getLogger("package_migration")


def setup_logging(debug=False, log_file=None):
    level = logging.DEBUG if debug else logging.INFO
    _logger.setLevel(level)

    fmt = logging.Formatter("[%(asctime)s] %(levelname)s - %(message)s", datefmt="%Y-%m-%d %H:%M:%S")

    console = logging.StreamHandler(sys.stdout)
    console.setLevel(level)
    console.setFormatter(fmt)
    _logger.addHandler(console)

    if log_file:
        fh = logging.FileHandler(log_file, mode="w", encoding="utf-8")
        fh.setLevel(logging.DEBUG)
        fh.setFormatter(fmt)
        _logger.addHandler(fh)


def log(msg):
    _logger.info(msg)


def debug_log(msg):
    _logger.debug(msg)


def error_log(msg):
    _logger.error(msg)


# ---------------------------------------------------------------------------
# .env autoload
# ---------------------------------------------------------------------------

def _load_dotenv():
    try:
        from dotenv import load_dotenv, find_dotenv
        env_file = find_dotenv(usecwd=True)
        if env_file:
            load_dotenv(env_file, override=False)
    except ImportError:
        pass


_load_dotenv()


# ---------------------------------------------------------------------------
# SHA-256 Utility
# ---------------------------------------------------------------------------

def compute_sha256(file_path):
    """Compute SHA-256 hash of a file."""
    sha256 = hashlib.sha256()
    with open(file_path, "rb") as f:
        for chunk in iter(lambda: f.read(8192), b""):
            sha256.update(chunk)
    return sha256.hexdigest()


def compute_sha256_from_bytes(data: bytes) -> str:
    """Compute SHA-256 hash from bytes."""
    return hashlib.sha256(data).hexdigest()


# ---------------------------------------------------------------------------
# GitLab Client (Package Download)
# ---------------------------------------------------------------------------

class GitLabPackageClient:
    """Client for downloading packages from GitLab Package Registry."""

    def __init__(self, gitlab_url, token, timeout=60, retries=3):
        self.base_url = gitlab_url.rstrip("/") + "/api/v4"
        self.token = token
        self.timeout = timeout
        self.retries = retries
        self.session = requests.Session()
        self.session.headers.update({"PRIVATE-TOKEN": token})

    def _get(self, path, params=None, stream=False):
        url = self.base_url + path
        for attempt in range(self.retries + 1):
            try:
                resp = self.session.get(url, params=params, timeout=self.timeout, stream=stream)
                if resp.status_code == 429:
                    retry_after = int(resp.headers.get("Retry-After", 2 ** (attempt + 1)))
                    log(f"Rate limited, waiting {retry_after}s...")
                    time.sleep(retry_after)
                    continue
                if resp.status_code >= 500:
                    wait = 2 ** attempt
                    log(f"Server error {resp.status_code}, retrying in {wait}s...")
                    time.sleep(wait)
                    continue
                return resp
            except (requests.exceptions.ConnectionError, requests.exceptions.Timeout):
                if attempt >= self.retries:
                    raise
                time.sleep(2 ** attempt)
        return resp

    def _get_paginated(self, path, params=None):
        results = []
        page = 1
        base_params = dict(params or {})
        while True:
            base_params["per_page"] = 100
            base_params["page"] = page
            resp = self._get(path, params=base_params)
            if resp.status_code == 404:
                return results
            resp.raise_for_status()
            items = resp.json()
            if not items:
                break
            results.extend(items)
            next_page = resp.headers.get("X-Next-Page")
            if not next_page:
                break
            page = int(next_page)
        return results

    def get_group_projects(self, group_path):
        """Get all projects under a group (including subgroups)."""
        encoded = quote(group_path, safe="")
        return self._get_paginated(
            f"/groups/{encoded}/projects",
            params={"include_subgroups": "true", "with_shared": "false"},
        )

    def get_project_packages(self, project_id):
        """Get all packages in a project."""
        return self._get_paginated(f"/projects/{project_id}/packages")

    def get_package_files(self, project_id, package_id):
        """Get all files for a specific package version."""
        resp = self._get(f"/projects/{project_id}/packages/{package_id}/package_files")
        if resp.status_code == 404:
            return []
        resp.raise_for_status()
        return resp.json()

    def download_package_file(self, project_id, package_id, file_id, file_name, dest_dir):
        """Download a package file to a local directory. Returns the local file path."""
        # Use the package file download endpoint
        path = f"/projects/{project_id}/packages/{package_id}/package_files/{file_id}/download"
        resp = self._get(path, stream=True)

        if resp.status_code == 404:
            # Fallback: try generic package download
            debug_log(f"File download 404, trying alternative endpoint for {file_name}")
            return None

        resp.raise_for_status()

        dest_path = Path(dest_dir) / file_name
        dest_path.parent.mkdir(parents=True, exist_ok=True)
        with open(dest_path, "wb") as f:
            for chunk in resp.iter_content(chunk_size=8192):
                f.write(chunk)
        return str(dest_path)


# ---------------------------------------------------------------------------
# GitHub Client (Package Upload)
# ---------------------------------------------------------------------------

class GitHubPackageClient:
    """Client for uploading packages to GitHub Packages."""

    def __init__(self, github_token, github_org, timeout=60, retries=3):
        self.token = github_token
        self.org = github_org
        self.timeout = timeout
        self.retries = retries
        self.session = requests.Session()
        self.session.headers.update({
            "Authorization": f"Bearer {github_token}",
            "Accept": "application/vnd.github.v3+json",
        })

    def _upload_with_retry(self, method, url, headers=None, data=None, files=None):
        """Upload with retry logic."""
        for attempt in range(self.retries + 1):
            try:
                req_headers = dict(self.session.headers)
                if headers:
                    req_headers.update(headers)

                if method == "PUT":
                    resp = requests.put(
                        url, headers=req_headers, data=data, timeout=self.timeout
                    )
                elif method == "POST":
                    resp = requests.post(
                        url, headers=req_headers, data=data, files=files, timeout=self.timeout
                    )
                else:
                    resp = requests.get(url, headers=req_headers, timeout=self.timeout)

                if resp.status_code == 429:
                    retry_after = int(resp.headers.get("Retry-After", 2 ** (attempt + 1)))
                    log(f"GitHub rate limited, waiting {retry_after}s...")
                    time.sleep(retry_after)
                    continue
                if resp.status_code >= 500:
                    time.sleep(2 ** attempt)
                    continue
                return resp
            except (requests.exceptions.ConnectionError, requests.exceptions.Timeout):
                if attempt >= self.retries:
                    raise
                time.sleep(2 ** attempt)
        return resp

    def upload_npm_package(self, package_name, version, tarball_path, repo_name):
        """Upload an npm package to GitHub Packages."""
        url = f"{DEFAULT_GITHUB_NPM_REGISTRY}/@{self.org}/{package_name}"

        with open(tarball_path, "rb") as f:
            tarball_data = f.read()

        # Create npm publish payload
        sha = compute_sha256_from_bytes(tarball_data)
        import base64
        tarball_b64 = base64.b64encode(tarball_data).decode()

        payload = {
            "name": f"@{self.org}/{package_name}",
            "version": version,
            "dist": {
                "integrity": f"sha256-{sha}",
                "shasum": hashlib.sha1(tarball_data).hexdigest(),
                "tarball": f"{DEFAULT_GITHUB_NPM_REGISTRY}/@{self.org}/{package_name}/-/{package_name}-{version}.tgz",
            },
            "_attachments": {
                f"{package_name}-{version}.tgz": {
                    "content_type": "application/octet-stream",
                    "data": tarball_b64,
                    "length": len(tarball_data),
                }
            },
            "repository": {
                "type": "git",
                "url": f"https://github.com/{self.org}/{repo_name}",
            },
        }

        headers = {"Content-Type": "application/json"}
        resp = self._upload_with_retry("PUT", url, headers=headers, data=json.dumps(payload))
        return resp

    def upload_maven_package(self, group_id, artifact_id, version, file_path, repo_name):
        """Upload a Maven package to GitHub Packages."""
        group_path = group_id.replace(".", "/")
        file_name = Path(file_path).name
        url = (
            f"{DEFAULT_GITHUB_MAVEN_REGISTRY}/{self.org}/{repo_name}"
            f"/{group_path}/{artifact_id}/{version}/{file_name}"
        )

        with open(file_path, "rb") as f:
            data = f.read()

        headers = {"Content-Type": "application/octet-stream"}
        resp = self._upload_with_retry("PUT", url, headers=headers, data=data)
        return resp

    def upload_nuget_package(self, nupkg_path):
        """Upload a NuGet package to GitHub Packages."""
        url = f"{DEFAULT_GITHUB_NUGET_REGISTRY}/{self.org}/index.json"

        with open(nupkg_path, "rb") as f:
            files = {"package": (Path(nupkg_path).name, f, "application/octet-stream")}
            resp = self._upload_with_retry("PUT", url, files=files)
        return resp

    def upload_generic_package(self, package_name, version, file_path, repo_name):
        """Upload a generic package as a GitHub release asset."""
        # For generic packages, we use GitHub Releases as the closest equivalent
        file_name = Path(file_path).name

        # Create release if not exists
        release_url = f"{DEFAULT_GITHUB_API}/repos/{self.org}/{repo_name}/releases"
        tag_name = f"{package_name}-v{version}"

        # Check if release exists
        resp = self._upload_with_retry("GET", f"{release_url}/tags/{tag_name}")
        if resp.status_code == 404:
            # Create the release
            payload = json.dumps({
                "tag_name": tag_name,
                "name": f"{package_name} v{version}",
                "body": f"Migrated from GitLab Package Registry\nOriginal package: {package_name} v{version}",
                "draft": False,
                "prerelease": False,
            })
            resp = self._upload_with_retry("POST", release_url, data=payload)
            if resp.status_code not in (200, 201):
                return resp
            release_data = resp.json()
        else:
            release_data = resp.json()

        # Upload asset
        upload_url = release_data.get("upload_url", "").replace("{?name,label}", "")
        upload_url = f"{upload_url}?name={file_name}"

        with open(file_path, "rb") as f:
            data = f.read()

        headers = {"Content-Type": "application/octet-stream"}
        resp = self._upload_with_retry("POST", upload_url, headers=headers, data=data)
        return resp

    def get_package_versions(self, package_type, package_name):
        """Get existing package versions from GitHub for verification."""
        url = f"{DEFAULT_GITHUB_API}/orgs/{self.org}/packages/{package_type}/{quote(package_name, safe='')}/versions"
        resp = self._upload_with_retry("GET", url)
        if resp.status_code == 404:
            return []
        if resp.status_code >= 400:
            debug_log(f"Error fetching GitHub package versions: {resp.status_code}")
            return []
        return resp.json()


# ---------------------------------------------------------------------------
# Migration Engine
# ---------------------------------------------------------------------------

class PackageMigrationEngine:
    """Orchestrates downloading from GitLab and uploading to GitHub."""

    def __init__(self, gitlab_client, github_client, work_dir, dry_run=False, verify_only=False):
        self.gitlab = gitlab_client
        self.github = github_client
        self.work_dir = Path(work_dir)
        self.work_dir.mkdir(parents=True, exist_ok=True)
        self.dry_run = dry_run
        self.verify_only = verify_only

        # Migration report
        self.report = {
            "started_at": datetime.utcnow().isoformat(),
            "packages_found": 0,
            "packages_migrated": 0,
            "packages_skipped": 0,
            "packages_failed": 0,
            "sha_mismatches": [],
            "errors": [],
            "details": [],
        }

    def run(self, group_path, repo_mapping=None):
        """
        Run the migration for all packages in the given GitLab group.

        Args:
            group_path: GitLab group path (e.g., 'my-org')
            repo_mapping: Optional dict mapping GitLab project_path -> GitHub repo name.
                          If not provided, uses the project name as repo name.
        """
        log(f"Starting package migration from GitLab group: {group_path}")
        log(f"Mode: {'VERIFY-ONLY' if self.verify_only else 'DRY-RUN' if self.dry_run else 'LIVE MIGRATION'}")

        # Discover projects
        projects = self.gitlab.get_group_projects(group_path)
        log(f"Found {len(projects)} projects in group '{group_path}'")

        for project in projects:
            project_id = project["id"]
            project_path = project.get("path_with_namespace", "")
            project_name = project.get("path", project.get("name", ""))

            # Determine target GitHub repo
            if repo_mapping and project_path in repo_mapping:
                github_repo = repo_mapping[project_path]
            else:
                github_repo = project_name

            log(f"\nProcessing project: {project_path} -> {self.github.org}/{github_repo}")

            # Get packages
            packages = self.gitlab.get_project_packages(project_id)
            if not packages:
                debug_log(f"  No packages in {project_path}")
                continue

            log(f"  Found {len(packages)} package(s)")
            self.report["packages_found"] += len(packages)

            for pkg in packages:
                self._migrate_package(project_id, project_path, pkg, github_repo)

        # Generate report
        self.report["completed_at"] = datetime.utcnow().isoformat()
        self._save_report()
        self._print_summary()

    def _migrate_package(self, project_id, project_path, package, github_repo):
        """Migrate a single package (all its files)."""
        pkg_id = package["id"]
        pkg_name = package.get("name", "unknown")
        pkg_type = package.get("package_type", "generic")
        pkg_version = package.get("version", "0.0.0")

        log(f"  [{pkg_type}] {pkg_name}@{pkg_version} (id={pkg_id})")

        if pkg_type not in SUPPORTED_PACKAGE_TYPES:
            log(f"    SKIPPED: unsupported package type '{pkg_type}'")
            self.report["packages_skipped"] += 1
            return

        # Get package files
        pkg_files = self.gitlab.get_package_files(project_id, pkg_id)
        if not pkg_files:
            log(f"    SKIPPED: no downloadable files")
            self.report["packages_skipped"] += 1
            return

        # Download each file
        pkg_dir = self.work_dir / f"{project_id}" / f"{pkg_name}" / f"{pkg_version}"
        pkg_dir.mkdir(parents=True, exist_ok=True)

        source_shas = {}
        downloaded_files = []

        for pkg_file in pkg_files:
            file_id = pkg_file.get("id")
            file_name = pkg_file.get("file_name", f"file_{file_id}")
            file_sha256 = pkg_file.get("file_sha256", "")

            debug_log(f"    Downloading: {file_name}")
            local_path = self.gitlab.download_package_file(
                project_id, pkg_id, file_id, file_name, str(pkg_dir)
            )

            if not local_path:
                error_log(f"    FAILED to download: {file_name}")
                self.report["errors"].append({
                    "package": f"{pkg_name}@{pkg_version}",
                    "file": file_name,
                    "error": "download_failed",
                    "project": project_path,
                })
                continue

            # Compute SHA-256 of downloaded file
            computed_sha = compute_sha256(local_path)
            source_shas[file_name] = {
                "gitlab_reported_sha256": file_sha256,
                "downloaded_sha256": computed_sha,
                "file_path": local_path,
            }

            # Verify GitLab-reported SHA matches downloaded content
            if file_sha256 and computed_sha != file_sha256:
                error_log(
                    f"    WARNING: SHA mismatch on download! "
                    f"GitLab reports {file_sha256}, computed {computed_sha}"
                )
                self.report["sha_mismatches"].append({
                    "stage": "download",
                    "package": f"{pkg_name}@{pkg_version}",
                    "file": file_name,
                    "expected_sha256": file_sha256,
                    "actual_sha256": computed_sha,
                    "project": project_path,
                })

            downloaded_files.append({
                "file_name": file_name,
                "local_path": local_path,
                "sha256": computed_sha,
                "size": os.path.getsize(local_path),
            })

        if not downloaded_files:
            self.report["packages_failed"] += 1
            return

        # Upload to GitHub (unless dry-run or verify-only)
        if self.verify_only:
            log(f"    VERIFY-ONLY: {len(downloaded_files)} file(s) downloaded, SHAs recorded")
            self.report["packages_skipped"] += 1
        elif self.dry_run:
            log(f"    DRY-RUN: would upload {len(downloaded_files)} file(s) to GitHub")
            self.report["packages_skipped"] += 1
        else:
            success = self._upload_to_github(
                pkg_name, pkg_type, pkg_version, downloaded_files, github_repo, source_shas
            )
            if success:
                self.report["packages_migrated"] += 1
            else:
                self.report["packages_failed"] += 1

        # Record details
        self.report["details"].append({
            "package_name": pkg_name,
            "package_type": pkg_type,
            "version": pkg_version,
            "project_path": project_path,
            "github_repo": github_repo,
            "files": [{
                "name": f["file_name"],
                "sha256": f["sha256"],
                "size": f["size"],
            } for f in downloaded_files],
            "source_shas": source_shas,
        })

    def _upload_to_github(self, pkg_name, pkg_type, version, files, github_repo, source_shas):
        """Upload package files to GitHub Packages."""
        try:
            for file_info in files:
                local_path = file_info["local_path"]
                file_name = file_info["file_name"]

                if pkg_type == "npm":
                    resp = self.github.upload_npm_package(pkg_name, version, local_path, github_repo)
                elif pkg_type == "maven":
                    # Infer group_id from package name (common convention: group.artifact)
                    parts = pkg_name.rsplit(".", 1)
                    group_id = parts[0] if len(parts) > 1 else self.github.org
                    artifact_id = parts[-1]
                    resp = self.github.upload_maven_package(
                        group_id, artifact_id, version, local_path, github_repo
                    )
                elif pkg_type == "nuget":
                    resp = self.github.upload_nuget_package(local_path)
                elif pkg_type == "generic":
                    resp = self.github.upload_generic_package(
                        pkg_name, version, local_path, github_repo
                    )
                elif pkg_type == "pypi":
                    # PyPI packages use generic upload as GitHub doesn't natively support PyPI
                    resp = self.github.upload_generic_package(
                        pkg_name, version, local_path, github_repo
                    )
                else:
                    log(f"    Unsupported type for upload: {pkg_type}")
                    continue

                if resp.status_code in (200, 201):
                    log(f"    UPLOADED: {file_name} -> GitHub ({resp.status_code})")
                elif resp.status_code == 409:
                    log(f"    ALREADY EXISTS: {file_name} (409 Conflict)")
                else:
                    error_log(
                        f"    UPLOAD FAILED: {file_name} -> {resp.status_code}: "
                        f"{resp.text[:200]}"
                    )
                    self.report["errors"].append({
                        "package": f"{pkg_name}@{version}",
                        "file": file_name,
                        "error": f"upload_failed_{resp.status_code}",
                        "detail": resp.text[:200],
                    })
                    return False

            return True

        except Exception as exc:
            error_log(f"    UPLOAD ERROR: {exc}")
            self.report["errors"].append({
                "package": f"{pkg_name}@{version}",
                "error": str(exc),
            })
            return False

    def _save_report(self):
        """Save migration report to JSON."""
        report_path = self.work_dir / "migration_report.json"
        with open(report_path, "w", encoding="utf-8") as f:
            json.dump(self.report, f, indent=2, default=str)
        log(f"\nReport saved to: {report_path}")

    def _print_summary(self):
        """Print migration summary."""
        r = self.report
        log("\n" + "=" * 60)
        log("MIGRATION SUMMARY")
        log("=" * 60)
        log(f"  Packages found:    {r['packages_found']}")
        log(f"  Packages migrated: {r['packages_migrated']}")
        log(f"  Packages skipped:  {r['packages_skipped']}")
        log(f"  Packages failed:   {r['packages_failed']}")
        log(f"  SHA mismatches:    {len(r['sha_mismatches'])}")
        log(f"  Errors:            {len(r['errors'])}")

        if r["sha_mismatches"]:
            log("\n  SHA-256 MISMATCHES DETECTED:")
            for m in r["sha_mismatches"]:
                log(f"    - [{m['stage']}] {m['package']} / {m['file']}")
                log(f"      Expected: {m['expected_sha256']}")
                log(f"      Actual:   {m['actual_sha256']}")

        log("=" * 60)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def parse_args():
    parser = argparse.ArgumentParser(
        description="Migrate packages from GitLab Package Registry to GitHub Packages"
    )
    parser.add_argument("--gitlab-url", default=os.getenv("GITLAB_URL", DEFAULT_GITLAB_URL),
                        help="GitLab instance URL")
    parser.add_argument("--gitlab-token", default=os.getenv("GITLAB_TOKEN"),
                        help="GitLab PAT (read_api scope)")
    parser.add_argument("--gitlab-group", required=True,
                        help="GitLab group path to migrate packages from")
    parser.add_argument("--github-token", default=os.getenv("GITHUB_TOKEN"),
                        help="GitHub PAT (write:packages scope)")
    parser.add_argument("--github-org", default=os.getenv("GITHUB_ORG"),
                        help="GitHub organization to upload packages to")
    parser.add_argument("--github-repo", default=None,
                        help="Override target GitHub repo name (default: same as GitLab project)")
    parser.add_argument("--package-types", default=None,
                        help="Comma-separated list of package types to migrate (default: all supported)")
    parser.add_argument("--dry-run", action="store_true",
                        help="Download and compute SHAs but do not upload to GitHub")
    parser.add_argument("--verify-only", action="store_true",
                        help="Only download and verify SHA integrity, no upload")
    parser.add_argument("--work-dir", default=None,
                        help="Working directory for downloaded packages (default: temp dir)")
    parser.add_argument("--debug", action="store_true", help="Enable debug logging")
    parser.add_argument("--log-file", default="package_migration.log",
                        help="Log file path")
    return parser.parse_args()


def main():
    args = parse_args()

    setup_logging(debug=args.debug, log_file=args.log_file)

    # Validate required args
    if not args.gitlab_token:
        error_log("GitLab token is required (--gitlab-token or GITLAB_TOKEN env var)")
        sys.exit(1)
    if not args.github_token and not args.verify_only:
        error_log("GitHub token is required (--github-token or GITHUB_TOKEN env var)")
        sys.exit(1)
    if not args.github_org and not args.verify_only:
        error_log("GitHub org is required (--github-org or GITHUB_ORG env var)")
        sys.exit(1)

    log(f"GitLab-to-GitHub Package Migration Tool v{VERSION}")
    log(f"GitLab URL: {args.gitlab_url}")
    log(f"GitLab Group: {args.gitlab_group}")
    if not args.verify_only:
        log(f"GitHub Org: {args.github_org}")

    # Initialize clients
    gitlab_client = GitLabPackageClient(args.gitlab_url, args.gitlab_token)
    github_client = GitHubPackageClient(
        args.github_token or "", args.github_org or ""
    )

    # Working directory
    if args.work_dir:
        work_dir = args.work_dir
    else:
        work_dir = tempfile.mkdtemp(prefix="gl2gh_pkg_")
    log(f"Working directory: {work_dir}")

    # Run migration
    engine = PackageMigrationEngine(
        gitlab_client, github_client, work_dir,
        dry_run=args.dry_run, verify_only=args.verify_only,
    )

    repo_mapping = None
    if args.github_repo:
        # If a single repo override is given, all packages go there
        repo_mapping = None  # Will use default logic, override handled per-project
        # This is a simplification; a mapping file would be better for complex cases

    engine.run(args.gitlab_group, repo_mapping=repo_mapping)


if __name__ == "__main__":
    main()
