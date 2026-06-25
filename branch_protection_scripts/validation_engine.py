"""Validation engine: verifies branch protection was applied correctly on GitHub."""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

from .github_client import GitHubClient

log = logging.getLogger(__name__)


@dataclass
class ValidationResult:
    """Result of validating a single branch protection rule."""
    github_repo: str
    branch: str
    status: str = ""  # "pass", "fail", "error"
    checks: list[dict] = field(default_factory=list)  # [{name, expected, actual, pass}]
    message: str = ""


class ValidationEngine:
    """Validates migrated branch protection rules on GitHub."""

    def __init__(self, config: dict[str, Any], github: GitHubClient):
        self.config = config
        self.github = github
        self.validation_config = config.get("validation", {})

    def validate(self, owner: str, repo: str, branch: str,
                 expected_payload: dict) -> ValidationResult:
        """Validate that GitHub branch protection matches expected configuration.

        Args:
            owner: GitHub org/owner.
            repo: GitHub repository name.
            branch: Branch name.
            expected_payload: The payload that was sent to GitHub API.

        Returns:
            ValidationResult with detailed check results.
        """
        result = ValidationResult(github_repo=f"{owner}/{repo}", branch=branch)

        # Check repo exists
        if not self.github.verify_repo_exists(owner, repo):
            result.status = "error"
            result.message = "Repository not found"
            return result

        # Check branch exists
        gh_branch = self.github.get_branch(owner, repo, branch)
        if gh_branch is None:
            result.status = "error"
            result.message = "Branch not found"
            return result

        # Get current protection
        protection = self.github.get_branch_protection(owner, repo, branch)
        if protection is None:
            result.status = "fail"
            result.message = "No branch protection found"
            return result

        # Run checks
        checks = []
        checks.append(self._check_enforce_admins(protection, expected_payload))
        checks.append(self._check_force_pushes(protection, expected_payload))
        checks.append(self._check_deletions(protection, expected_payload))
        checks.append(self._check_pr_reviews(protection, expected_payload))
        checks.append(self._check_status_checks(protection, expected_payload))
        checks.append(self._check_restrictions(protection, expected_payload))

        result.checks = [c for c in checks if c is not None]
        all_pass = all(c["pass"] for c in result.checks)
        result.status = "pass" if all_pass else "fail"
        if not all_pass:
            failed = [c["name"] for c in result.checks if not c["pass"]]
            result.message = f"Failed checks: {', '.join(failed)}"

        return result

    def validate_batch(self, results: list[dict]) -> list[ValidationResult]:
        """Validate a batch of migration results.

        Args:
            results: List of dicts with github_owner, github_repo, branch, github_payload.

        Returns:
            List of ValidationResult objects.
        """
        validations = []
        for r in results:
            if r.get("status") not in ("success", "dry_run"):
                continue
            v = self.validate(
                r["github_owner"], r["github_repo"],
                r["branch"], r.get("github_payload", {}),
            )
            validations.append(v)
        return validations

    def _check_enforce_admins(self, actual: dict, expected: dict) -> dict:
        expected_val = expected.get("enforce_admins", False)
        actual_val = actual.get("enforce_admins", {}).get("enabled", False)
        return {
            "name": "enforce_admins",
            "expected": expected_val,
            "actual": actual_val,
            "pass": expected_val == actual_val,
        }

    def _check_force_pushes(self, actual: dict, expected: dict) -> dict:
        expected_val = expected.get("allow_force_pushes", False)
        actual_val = actual.get("allow_force_pushes", {}).get("enabled", False)
        return {
            "name": "allow_force_pushes",
            "expected": expected_val,
            "actual": actual_val,
            "pass": expected_val == actual_val,
        }

    def _check_deletions(self, actual: dict, expected: dict) -> dict:
        expected_val = expected.get("allow_deletions", False)
        actual_val = actual.get("allow_deletions", {}).get("enabled", False)
        return {
            "name": "allow_deletions",
            "expected": expected_val,
            "actual": actual_val,
            "pass": expected_val == actual_val,
        }

    def _check_pr_reviews(self, actual: dict, expected: dict) -> dict | None:
        expected_reviews = expected.get("required_pull_request_reviews")
        if expected_reviews is None:
            return None

        actual_reviews = actual.get("required_pull_request_reviews", {})
        expected_count = expected_reviews.get("required_approving_review_count", 1)
        actual_count = actual_reviews.get("required_approving_review_count", 0)
        return {
            "name": "required_approving_reviews",
            "expected": expected_count,
            "actual": actual_count,
            "pass": expected_count == actual_count,
        }

    def _check_status_checks(self, actual: dict, expected: dict) -> dict | None:
        expected_checks = expected.get("required_status_checks")
        if expected_checks is None:
            # No status checks required
            actual_checks = actual.get("required_status_checks")
            return {
                "name": "required_status_checks",
                "expected": None,
                "actual": actual_checks is not None,
                "pass": actual_checks is None,
            }
        expected_contexts = set(expected_checks.get("contexts", []))
        actual_obj = actual.get("required_status_checks", {})
        actual_contexts = set(actual_obj.get("contexts", [])) if actual_obj else set()
        return {
            "name": "required_status_checks",
            "expected": sorted(expected_contexts),
            "actual": sorted(actual_contexts),
            "pass": expected_contexts == actual_contexts,
        }

    def _check_restrictions(self, actual: dict, expected: dict) -> dict | None:
        expected_restrictions = expected.get("restrictions")
        actual_restrictions = actual.get("restrictions")
        has_expected = expected_restrictions is not None
        has_actual = actual_restrictions is not None
        return {
            "name": "push_restrictions",
            "expected": has_expected,
            "actual": has_actual,
            "pass": has_expected == has_actual,
        }
