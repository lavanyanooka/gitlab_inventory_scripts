"""GitHub REST API client for branch protection rules."""

from __future__ import annotations

import logging
import time
from typing import Any

import requests

log = logging.getLogger(__name__)


class GitHubClient:
    """GitHub API client with rate limiting, retry, and branch protection support."""

    def __init__(self, token: str, api_url: str = "https://api.github.com",
                 max_retries: int = 3, backoff_factor: float = 2.0):
        self.api_url = api_url.rstrip("/")
        self.max_retries = max_retries
        self.backoff_factor = backoff_factor
        self.session = requests.Session()
        self.session.headers.update({
            "Authorization": f"token {token}",
            "Accept": "application/vnd.github.v3+json",
        })

    def _request(self, method: str, endpoint: str, **kwargs) -> requests.Response:
        """Make API request with retry and rate limit handling."""
        url = f"{self.api_url}{endpoint}" if endpoint.startswith("/") else endpoint
        for attempt in range(self.max_retries + 1):
            try:
                resp = self.session.request(method, url, **kwargs)
                if resp.status_code == 403:
                    remaining = resp.headers.get("X-RateLimit-Remaining", "1")
                    if remaining == "0":
                        reset_ts = int(resp.headers.get("X-RateLimit-Reset", 0))
                        wait = max(reset_ts - int(time.time()), 1)
                        log.warning(f"Rate limited. Waiting {wait}s (attempt {attempt + 1})")
                        time.sleep(wait)
                        continue
                if resp.status_code == 429:
                    retry_after = int(resp.headers.get("Retry-After", 60))
                    log.warning(f"Secondary rate limit. Waiting {retry_after}s")
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

    def _put(self, endpoint: str, json: dict | None = None) -> requests.Response:
        return self._request("PUT", endpoint, json=json)

    def _delete(self, endpoint: str) -> requests.Response:
        return self._request("DELETE", endpoint)

    def verify_repo_exists(self, owner: str, repo: str) -> bool:
        """Check if a GitHub repo exists and is accessible."""
        resp = self._get(f"/repos/{owner}/{repo}")
        return resp.status_code == 200

    def get_branch(self, owner: str, repo: str, branch: str) -> dict | None:
        """Get branch details. Returns None if not found."""
        resp = self._get(f"/repos/{owner}/{repo}/branches/{branch}")
        if resp.status_code == 404:
            return None
        resp.raise_for_status()
        return resp.json()

    def get_branch_protection(self, owner: str, repo: str, branch: str) -> dict | None:
        """Get current branch protection rules. Returns None if not protected."""
        resp = self._get(f"/repos/{owner}/{repo}/branches/{branch}/protection")
        if resp.status_code == 404:
            return None
        resp.raise_for_status()
        return resp.json()

    def set_branch_protection(self, owner: str, repo: str, branch: str,
                              protection: dict) -> dict:
        """Apply branch protection rules."""
        resp = self._put(
            f"/repos/{owner}/{repo}/branches/{branch}/protection",
            json=protection,
        )
        resp.raise_for_status()
        return resp.json()

    def delete_branch_protection(self, owner: str, repo: str, branch: str) -> bool:
        """Remove branch protection. Returns True if successful."""
        resp = self._delete(f"/repos/{owner}/{repo}/branches/{branch}/protection")
        return resp.status_code == 204

    def get_teams(self, org: str) -> list[dict]:
        """Get all teams in an org."""
        results = []
        page = 1
        while True:
            resp = self._get(f"/orgs/{org}/teams", params={"per_page": 100, "page": page})
            resp.raise_for_status()
            data = resp.json()
            if not data:
                break
            results.extend(data)
            page += 1
        return results
