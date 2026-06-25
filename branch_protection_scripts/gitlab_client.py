"""GitLab REST API client for branch protection rules."""

from __future__ import annotations

import logging
import time
from typing import Any
from urllib.parse import quote

import requests

log = logging.getLogger(__name__)


class GitLabClient:
    """GitLab API client with pagination, rate limiting, and retry support."""

    def __init__(self, base_url: str, token: str, per_page: int = 100,
                 max_retries: int = 3, backoff_factor: float = 2.0):
        self.base_url = base_url.rstrip("/")
        self.api_url = f"{self.base_url}/api/v4"
        self.per_page = per_page
        self.max_retries = max_retries
        self.backoff_factor = backoff_factor
        self.session = requests.Session()
        self.session.headers.update({"PRIVATE-TOKEN": token})

    def _request(self, method: str, endpoint: str, **kwargs) -> requests.Response:
        """Make API request with retry and rate limit handling."""
        url = f"{self.api_url}{endpoint}" if endpoint.startswith("/") else endpoint
        for attempt in range(self.max_retries + 1):
            try:
                resp = self.session.request(method, url, **kwargs)
                if resp.status_code == 429:
                    retry_after = int(resp.headers.get("Retry-After", 60))
                    log.warning(f"Rate limited. Waiting {retry_after}s (attempt {attempt + 1})")
                    time.sleep(retry_after)
                    continue
                if resp.status_code >= 500 and attempt < self.max_retries:
                    wait = self.backoff_factor ** attempt
                    log.warning(f"Server error {resp.status_code}. Retrying in {wait}s")
                    time.sleep(wait)
                    continue
                return resp
            except requests.exceptions.ConnectionError as e:
                if attempt < self.max_retries:
                    wait = self.backoff_factor ** attempt
                    log.warning(f"Connection error: {e}. Retrying in {wait}s")
                    time.sleep(wait)
                else:
                    raise
        return resp

    def _get(self, endpoint: str, params: dict | None = None) -> requests.Response:
        return self._request("GET", endpoint, params=params)

    def _paginate(self, endpoint: str, params: dict | None = None) -> list[dict]:
        """Fetch all pages from a paginated endpoint."""
        params = params or {}
        params["per_page"] = self.per_page
        params["page"] = 1
        results = []
        while True:
            resp = self._get(endpoint, params=params)
            resp.raise_for_status()
            data = resp.json()
            if not data:
                break
            results.extend(data)
            next_page = resp.headers.get("x-next-page")
            if not next_page:
                break
            params["page"] = int(next_page)
        return results

    def get_project(self, project_id: int | str) -> dict:
        """Get project details."""
        if isinstance(project_id, str):
            project_id = quote(project_id, safe="")
        resp = self._get(f"/projects/{project_id}")
        resp.raise_for_status()
        return resp.json()

    def get_protected_branches(self, project_id: int | str) -> list[dict]:
        """Get all protected branches for a project."""
        if isinstance(project_id, str):
            project_id = quote(project_id, safe="")
        return self._paginate(f"/projects/{project_id}/protected_branches")

    def get_branch(self, project_id: int | str, branch_name: str) -> dict | None:
        """Get a specific branch. Returns None if not found."""
        if isinstance(project_id, str):
            project_id = quote(project_id, safe="")
        branch_name = quote(branch_name, safe="")
        resp = self._get(f"/projects/{project_id}/repository/branches/{branch_name}")
        if resp.status_code == 404:
            return None
        resp.raise_for_status()
        return resp.json()

    def get_approval_rules(self, project_id: int | str) -> list[dict]:
        """Get project-level approval rules."""
        if isinstance(project_id, str):
            project_id = quote(project_id, safe="")
        return self._paginate(f"/projects/{project_id}/approval_rules")

    def get_project_approval_config(self, project_id: int | str) -> dict:
        """Get project-level approval configuration."""
        if isinstance(project_id, str):
            project_id = quote(project_id, safe="")
        resp = self._get(f"/projects/{project_id}/approvals")
        resp.raise_for_status()
        return resp.json()

    def verify_project_exists(self, project_id: int | str) -> bool:
        """Check if a project exists and is accessible."""
        try:
            if isinstance(project_id, str):
                project_id = quote(project_id, safe="")
            resp = self._get(f"/projects/{project_id}")
            return resp.status_code == 200
        except Exception:
            return False
