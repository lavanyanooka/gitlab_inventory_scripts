"""Unit tests for the validation engine."""

import pytest
from unittest.mock import MagicMock

from branch_protection_scripts.validation_engine import ValidationEngine


@pytest.fixture
def config():
    return {"validation": {"enabled": True, "fail_on_mismatch": False}}


@pytest.fixture
def mock_github():
    return MagicMock()


@pytest.fixture
def engine(config, mock_github):
    return ValidationEngine(config, mock_github)


class TestValidationEngine:
    def test_repo_not_found(self, engine, mock_github):
        mock_github.verify_repo_exists.return_value = False
        result = engine.validate("owner", "repo", "main", {})
        assert result.status == "error"
        assert "not found" in result.message.lower()

    def test_branch_not_found(self, engine, mock_github):
        mock_github.verify_repo_exists.return_value = True
        mock_github.get_branch.return_value = None
        result = engine.validate("owner", "repo", "main", {})
        assert result.status == "error"
        assert "not found" in result.message.lower()

    def test_no_protection_found(self, engine, mock_github):
        mock_github.verify_repo_exists.return_value = True
        mock_github.get_branch.return_value = {"name": "main"}
        mock_github.get_branch_protection.return_value = None
        result = engine.validate("owner", "repo", "main", {})
        assert result.status == "fail"

    def test_all_checks_pass(self, engine, mock_github):
        mock_github.verify_repo_exists.return_value = True
        mock_github.get_branch.return_value = {"name": "main"}
        mock_github.get_branch_protection.return_value = {
            "enforce_admins": {"enabled": False},
            "allow_force_pushes": {"enabled": False},
            "allow_deletions": {"enabled": False},
            "required_pull_request_reviews": {"required_approving_review_count": 2},
            "required_status_checks": None,
            "restrictions": {"users": [], "teams": []},
        }
        expected_payload = {
            "enforce_admins": False,
            "allow_force_pushes": False,
            "allow_deletions": False,
            "required_pull_request_reviews": {"required_approving_review_count": 2},
            "required_status_checks": None,
            "restrictions": {"users": [], "teams": []},
        }
        result = engine.validate("owner", "repo", "main", expected_payload)
        assert result.status == "pass"

    def test_enforce_admins_mismatch(self, engine, mock_github):
        mock_github.verify_repo_exists.return_value = True
        mock_github.get_branch.return_value = {"name": "main"}
        mock_github.get_branch_protection.return_value = {
            "enforce_admins": {"enabled": True},
            "allow_force_pushes": {"enabled": False},
            "allow_deletions": {"enabled": False},
            "required_status_checks": None,
            "restrictions": None,
        }
        expected_payload = {
            "enforce_admins": False,
            "allow_force_pushes": False,
            "allow_deletions": False,
            "required_status_checks": None,
            "restrictions": None,
        }
        result = engine.validate("owner", "repo", "main", expected_payload)
        assert result.status == "fail"
        assert "enforce_admins" in result.message
