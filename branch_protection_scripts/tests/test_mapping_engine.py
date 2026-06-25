"""Unit tests for the mapping engine."""

import pytest
from branch_protection_scripts.mapping_engine import MappingEngine


@pytest.fixture
def config():
    return {
        "mapping": {
            "defaults": {
                "require_pull_request": True,
                "required_approving_reviews": 1,
                "dismiss_stale_reviews": True,
                "require_code_owner_reviews": False,
                "required_status_checks": [],
                "strict_status_checks": False,
                "enforce_admins": False,
                "require_linear_history": False,
                "require_signed_commits": False,
                "allow_force_pushes": False,
                "allow_deletions": False,
            }
        }
    }


@pytest.fixture
def engine(config):
    return MappingEngine(config)


class TestMappingEngine:
    def test_basic_protection_mapping(self, engine):
        gitlab_protection = {
            "name": "main",
            "push_access_levels": [{"access_level": 40}],
            "merge_access_levels": [{"access_level": 30}],
            "allow_force_push": False,
            "code_owner_approval_required": False,
        }
        payload = engine.map_protection(gitlab_protection)

        assert payload["allow_force_pushes"] is False
        assert payload["allow_deletions"] is False
        assert payload["enforce_admins"] is False
        assert payload["required_pull_request_reviews"] is not None
        assert payload["required_pull_request_reviews"]["required_approving_review_count"] == 1

    def test_force_push_allowed(self, engine):
        gitlab_protection = {
            "name": "develop",
            "push_access_levels": [{"access_level": 30}],
            "merge_access_levels": [{"access_level": 30}],
            "allow_force_push": True,
        }
        payload = engine.map_protection(gitlab_protection)
        assert payload["allow_force_pushes"] is True

    def test_code_owner_required(self, engine):
        gitlab_protection = {
            "name": "main",
            "push_access_levels": [{"access_level": 40}],
            "merge_access_levels": [{"access_level": 30}],
            "allow_force_push": False,
            "code_owner_approval_required": True,
        }
        payload = engine.map_protection(gitlab_protection)
        assert payload["required_pull_request_reviews"]["require_code_owner_reviews"] is True

    def test_approval_rules_override(self, engine):
        gitlab_protection = {
            "name": "main",
            "push_access_levels": [{"access_level": 40}],
            "merge_access_levels": [{"access_level": 30}],
            "allow_force_push": False,
        }
        approval_rules = [
            {"rule_type": "regular", "approvals_required": 3}
        ]
        payload = engine.map_protection(gitlab_protection, approval_rules=approval_rules)
        assert payload["required_pull_request_reviews"]["required_approving_review_count"] == 3

    def test_approval_count_capped_at_six(self, engine):
        gitlab_protection = {
            "name": "main",
            "push_access_levels": [],
            "merge_access_levels": [],
            "allow_force_push": False,
        }
        approval_rules = [
            {"rule_type": "regular", "approvals_required": 10}
        ]
        payload = engine.map_protection(gitlab_protection, approval_rules=approval_rules)
        assert payload["required_pull_request_reviews"]["required_approving_review_count"] == 6

    def test_restrictions_for_maintainer_only_push(self, engine):
        gitlab_protection = {
            "name": "main",
            "push_access_levels": [{"access_level": 40}],
            "merge_access_levels": [{"access_level": 30}],
            "allow_force_push": False,
        }
        payload = engine.map_protection(gitlab_protection)
        assert payload["restrictions"] is not None
        assert payload["restrictions"]["users"] == []
        assert payload["restrictions"]["teams"] == []

    def test_no_restrictions_for_developer_push(self, engine):
        gitlab_protection = {
            "name": "develop",
            "push_access_levels": [{"access_level": 30}, {"access_level": 60}],
            "merge_access_levels": [{"access_level": 30}],
            "allow_force_push": False,
        }
        payload = engine.map_protection(gitlab_protection)
        assert payload["restrictions"] is None

    def test_get_branch_name(self, engine):
        protection = {"name": "release/*", "other": "data"}
        assert engine.get_branch_name(protection) == "release/*"

    def test_no_pr_reviews_when_disabled(self, config):
        config["mapping"]["defaults"]["require_pull_request"] = False
        engine = MappingEngine(config)
        gitlab_protection = {
            "name": "main",
            "push_access_levels": [],
            "merge_access_levels": [],
            "allow_force_push": False,
        }
        payload = engine.map_protection(gitlab_protection)
        assert payload["required_pull_request_reviews"] is None
