"""Migration engine: orchestrates the branch protection migration workflow."""

from __future__ import annotations

import csv
import json
import logging
import re
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .gitlab_client import GitLabClient
from .github_client import GitHubClient
from .mapping_engine import MappingEngine

log = logging.getLogger(__name__)


@dataclass
class MigrationResult:
    """Result of migrating a single branch protection rule."""
    gitlab_project: str
    github_repo: str
    branch: str
    status: str  # "success", "failed", "skipped", "dry_run"
    message: str = ""
    duration_ms: int = 0
    retries: int = 0
    gitlab_config: dict = field(default_factory=dict)
    github_payload: dict = field(default_factory=dict)


class MigrationEngine:
    """Orchestrates the branch protection migration from GitLab to GitHub."""

    def __init__(self, config: dict[str, Any], gitlab: GitLabClient,
                 github: GitHubClient, mapping: MappingEngine):
        self.config = config
        self.gitlab = gitlab
        self.github = github
        self.mapping = mapping
        self.migration_config = config.get("migration", {})
        self.dry_run = self.migration_config.get("dry_run", False)
        self.skip_existing = self.migration_config.get("skip_existing", False)
        self.workers = self.migration_config.get("parallel_workers", 4)
        self.batch_size = self.migration_config.get("batch_size", 50)
        self.results: list[MigrationResult] = []
        self._state_file = Path("logs/.migration_state.json")

    def run(self, repo_mapping: list[dict], resume: bool = False) -> list[MigrationResult]:
        """Execute migration for all mapped repositories.

        Args:
            repo_mapping: List of dicts with gitlab_project_id, github_owner, github_repo.
            resume: If True, skip repos already processed in a previous run.

        Returns:
            List of MigrationResult objects.
        """
        completed_repos = set()
        if resume:
            completed_repos = self._load_state()
            log.info(f"Resuming migration. {len(completed_repos)} repos already processed.")

        # Apply include/exclude filters
        repo_mapping = self._filter_repos(repo_mapping)
        log.info(f"Processing {len(repo_mapping)} repository mappings (dry_run={self.dry_run})")

        # Process in batches with thread pool
        for batch_start in range(0, len(repo_mapping), self.batch_size):
            batch = repo_mapping[batch_start:batch_start + self.batch_size]
            with ThreadPoolExecutor(max_workers=self.workers) as executor:
                futures = {}
                for mapping in batch:
                    repo_key = f"{mapping['gitlab_project_id']}:{mapping['github_repo']}"
                    if repo_key in completed_repos:
                        log.debug(f"Skipping already-processed repo: {repo_key}")
                        continue
                    future = executor.submit(self._migrate_repo, mapping)
                    futures[future] = mapping

                for future in as_completed(futures):
                    mapping = futures[future]
                    try:
                        results = future.result()
                        self.results.extend(results)
                        # Save state after each repo
                        repo_key = f"{mapping['gitlab_project_id']}:{mapping['github_repo']}"
                        completed_repos.add(repo_key)
                        self._save_state(completed_repos)
                    except Exception as e:
                        log.error(f"Unhandled error for {mapping}: {e}")
                        self.results.append(MigrationResult(
                            gitlab_project=str(mapping.get("gitlab_project_id", "")),
                            github_repo=mapping.get("github_repo", ""),
                            branch="*",
                            status="failed",
                            message=str(e),
                        ))

        return self.results

    def _migrate_repo(self, mapping: dict) -> list[MigrationResult]:
        """Migrate branch protections for a single repository."""
        results = []
        gitlab_project_id = mapping["gitlab_project_id"]
        github_owner = mapping["github_owner"]
        github_repo = mapping["github_repo"]

        log.info(f"Processing: GitLab #{gitlab_project_id} -> {github_owner}/{github_repo}")

        # Verify GitLab project exists
        if not self.gitlab.verify_project_exists(gitlab_project_id):
            msg = f"GitLab project {gitlab_project_id} not found or inaccessible"
            log.warning(msg)
            results.append(MigrationResult(
                gitlab_project=str(gitlab_project_id),
                github_repo=f"{github_owner}/{github_repo}",
                branch="*", status="failed", message=msg,
            ))
            return results

        # Verify GitHub repo exists
        if not self.github.verify_repo_exists(github_owner, github_repo):
            msg = f"GitHub repo {github_owner}/{github_repo} not found or inaccessible"
            log.warning(msg)
            results.append(MigrationResult(
                gitlab_project=str(gitlab_project_id),
                github_repo=f"{github_owner}/{github_repo}",
                branch="*", status="failed", message=msg,
            ))
            return results

        # Get GitLab branch protections
        try:
            protections = self.gitlab.get_protected_branches(gitlab_project_id)
        except Exception as e:
            msg = f"Failed to fetch GitLab protections: {e}"
            log.error(msg)
            results.append(MigrationResult(
                gitlab_project=str(gitlab_project_id),
                github_repo=f"{github_owner}/{github_repo}",
                branch="*", status="failed", message=msg,
            ))
            return results

        # Get approval rules for richer mapping
        approval_rules = []
        approval_config = {}
        try:
            approval_rules = self.gitlab.get_approval_rules(gitlab_project_id)
            approval_config = self.gitlab.get_project_approval_config(gitlab_project_id)
        except Exception as e:
            log.debug(f"Could not fetch approval rules: {e}")

        # Apply branch include/exclude filters
        protections = self._filter_branches(protections)

        for protection in protections:
            branch_name = self.mapping.get_branch_name(protection)
            result = self._migrate_branch_protection(
                gitlab_project_id, github_owner, github_repo,
                branch_name, protection, approval_rules, approval_config,
            )
            results.append(result)

        return results

    def _migrate_branch_protection(
        self, gitlab_project_id: int | str, github_owner: str, github_repo: str,
        branch_name: str, protection: dict, approval_rules: list, approval_config: dict,
    ) -> MigrationResult:
        """Migrate a single branch protection rule."""
        start = time.time()

        # Check if branch exists on GitHub
        gh_branch = self.github.get_branch(github_owner, github_repo, branch_name)
        if gh_branch is None:
            duration = int((time.time() - start) * 1000)
            return MigrationResult(
                gitlab_project=str(gitlab_project_id),
                github_repo=f"{github_owner}/{github_repo}",
                branch=branch_name, status="skipped",
                message=f"Branch '{branch_name}' does not exist on GitHub",
                duration_ms=duration, gitlab_config=protection,
            )

        # Check if protection already exists
        if self.skip_existing:
            existing = self.github.get_branch_protection(github_owner, github_repo, branch_name)
            if existing:
                duration = int((time.time() - start) * 1000)
                return MigrationResult(
                    gitlab_project=str(gitlab_project_id),
                    github_repo=f"{github_owner}/{github_repo}",
                    branch=branch_name, status="skipped",
                    message="Protection already exists (skip_existing=true)",
                    duration_ms=duration, gitlab_config=protection,
                )

        # Map GitLab protection to GitHub payload
        payload = self.mapping.map_protection(protection, approval_rules, approval_config)

        if self.dry_run:
            duration = int((time.time() - start) * 1000)
            log.info(f"  [DRY RUN] Would apply protection to {github_owner}/{github_repo}:{branch_name}")
            return MigrationResult(
                gitlab_project=str(gitlab_project_id),
                github_repo=f"{github_owner}/{github_repo}",
                branch=branch_name, status="dry_run",
                message="Dry run - no changes applied",
                duration_ms=duration, gitlab_config=protection, github_payload=payload,
            )

        # Apply protection
        try:
            self.github.set_branch_protection(github_owner, github_repo, branch_name, payload)
            duration = int((time.time() - start) * 1000)
            log.info(f"  Applied protection to {github_owner}/{github_repo}:{branch_name}")
            return MigrationResult(
                gitlab_project=str(gitlab_project_id),
                github_repo=f"{github_owner}/{github_repo}",
                branch=branch_name, status="success",
                duration_ms=duration, gitlab_config=protection, github_payload=payload,
            )
        except Exception as e:
            duration = int((time.time() - start) * 1000)
            log.error(f"  Failed to apply protection: {e}")
            return MigrationResult(
                gitlab_project=str(gitlab_project_id),
                github_repo=f"{github_owner}/{github_repo}",
                branch=branch_name, status="failed",
                message=str(e), duration_ms=duration,
                gitlab_config=protection, github_payload=payload,
            )

    def _filter_repos(self, repo_mapping: list[dict]) -> list[dict]:
        """Apply include/exclude repo filters."""
        include = self.migration_config.get("include_repos", [])
        exclude = self.migration_config.get("exclude_repos", [])
        if not include and not exclude:
            return repo_mapping

        filtered = []
        for m in repo_mapping:
            repo_name = m.get("github_repo", "")
            if include and not any(re.search(p, repo_name) for p in include):
                continue
            if exclude and any(re.search(p, repo_name) for p in exclude):
                continue
            filtered.append(m)
        return filtered

    def _filter_branches(self, protections: list[dict]) -> list[dict]:
        """Apply include/exclude branch filters."""
        include = self.migration_config.get("include_branches", [])
        exclude = self.migration_config.get("exclude_branches", [])
        if not include and not exclude:
            return protections

        filtered = []
        for p in protections:
            name = p.get("name", "")
            if include and not any(re.search(pat, name) for pat in include):
                continue
            if exclude and any(re.search(pat, name) for pat in exclude):
                continue
            filtered.append(p)
        return filtered

    def _load_state(self) -> set[str]:
        """Load migration state for resume support."""
        if self._state_file.exists():
            data = json.loads(self._state_file.read_text())
            return set(data.get("completed", []))
        return set()

    def _save_state(self, completed: set[str]) -> None:
        """Save migration state."""
        self._state_file.parent.mkdir(parents=True, exist_ok=True)
        self._state_file.write_text(json.dumps({"completed": sorted(completed)}))


def load_repo_mapping(path: str | Path) -> list[dict]:
    """Load repository mapping from CSV or JSON.

    Expected fields: gitlab_project_id, github_owner, github_repo
    """
    path = Path(path)
    if path.suffix == ".json":
        with open(path) as f:
            return json.load(f)
    elif path.suffix == ".csv":
        with open(path, newline="") as f:
            reader = csv.DictReader(f)
            return [row for row in reader]
    else:
        raise ValueError(f"Unsupported mapping file format: {path.suffix}. Use .csv or .json")
