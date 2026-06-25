"""Mapping engine: transforms GitLab branch protection settings to GitHub API payloads."""

from __future__ import annotations

import logging
from typing import Any

log = logging.getLogger(__name__)

# GitLab access levels
GITLAB_ACCESS_LEVELS = {
    0: "no_access",
    30: "developer",
    40: "maintainer",
    60: "admin",
}


class MappingEngine:
    """Maps GitLab branch protection configuration to GitHub branch protection payload."""

    def __init__(self, config: dict[str, Any]):
        self.config = config
        self.mapping_config = config.get("mapping", {})
        self.defaults = self.mapping_config.get("defaults", {})

    def map_protection(self, gitlab_protection: dict, approval_rules: list[dict] | None = None,
                       approval_config: dict | None = None) -> dict:
        """Map a single GitLab protected branch to a GitHub protection payload.

        Args:
            gitlab_protection: GitLab protected branch API response.
            approval_rules: Project-level approval rules from GitLab.
            approval_config: Project-level approval configuration.

        Returns:
            GitHub branch protection API payload.
        """
        payload = {
            "required_status_checks": self._map_status_checks(gitlab_protection),
            "enforce_admins": self._map_enforce_admins(gitlab_protection),
            "required_pull_request_reviews": self._map_pr_reviews(
                gitlab_protection, approval_rules, approval_config
            ),
            "restrictions": self._map_restrictions(gitlab_protection),
            "required_linear_history": self.defaults.get("require_linear_history", False),
            "allow_force_pushes": self._map_force_push(gitlab_protection),
            "allow_deletions": self._map_deletions(gitlab_protection),
            "required_signatures": self.defaults.get("require_signed_commits", False),
        }
        return payload

    def _map_status_checks(self, protection: dict) -> dict | None:
        """Map required status checks."""
        status_checks = self.defaults.get("required_status_checks", [])
        if not status_checks and not self.defaults.get("strict_status_checks", False):
            return None
        return {
            "strict": self.defaults.get("strict_status_checks", False),
            "contexts": status_checks,
        }

    def _map_enforce_admins(self, protection: dict) -> bool:
        """Map admin enforcement."""
        return self.defaults.get("enforce_admins", False)

    def _map_pr_reviews(self, protection: dict, approval_rules: list[dict] | None,
                        approval_config: dict | None) -> dict | None:
        """Map pull request review requirements from GitLab approval rules."""
        if not self.defaults.get("require_pull_request", True):
            return None

        # Determine required approvals from GitLab
        required_approvals = self.defaults.get("required_approving_reviews", 1)
        if approval_config and approval_config.get("approvals_before_merge"):
            required_approvals = max(1, approval_config["approvals_before_merge"])
        if approval_rules:
            for rule in approval_rules:
                if rule.get("rule_type") == "regular":
                    required_approvals = max(required_approvals, rule.get("approvals_required", 1))

        # Check for code owner approval
        require_code_owners = self.defaults.get("require_code_owner_reviews", False)
        if protection.get("code_owner_approval_required"):
            require_code_owners = True

        return {
            "dismiss_stale_reviews": self.defaults.get("dismiss_stale_reviews", True),
            "require_code_owner_reviews": require_code_owners,
            "required_approving_review_count": min(required_approvals, 6),  # GitHub max is 6
        }

    def _map_restrictions(self, protection: dict) -> dict | None:
        """Map push restrictions.

        GitHub restrictions limit who can push. If GitLab allows only maintainers+,
        we set restrictions (empty = only admins can push).
        """
        push_access = protection.get("push_access_levels", [])
        if not push_access:
            return None

        # If push is restricted to maintainers only (access_level=40) or higher
        max_level = max((a.get("access_level", 0) or 0) for a in push_access) if push_access else 0
        if max_level <= 40:
            # Restricted: return empty restrictions (only admins/specified can push)
            return {"users": [], "teams": [], "apps": []}

        return None

    def _map_force_push(self, protection: dict) -> bool:
        """Map force push settings."""
        return protection.get("allow_force_push", False)

    def _map_deletions(self, protection: dict) -> bool:
        """Map branch deletion settings."""
        # GitLab doesn't have a direct equivalent in the protected_branches API
        # but some versions expose it
        return self.defaults.get("allow_deletions", False)

    def get_branch_name(self, gitlab_protection: dict) -> str:
        """Extract the branch name/pattern from a GitLab protection rule."""
        return gitlab_protection.get("name", "")
