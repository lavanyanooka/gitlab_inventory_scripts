#!/usr/bin/env python3
"""
Create Branch Protection Rules in GitLab
=========================================

Applies branch protection rules (from a YAML config) to all projects in a
GitLab group. Supports:
  - Multiple branch patterns per project
  - Dry-run mode
  - Skip already-protected branches
  - Parallel execution across projects
  - Token pool (reuses the shared gitlab_client module)
  - Checkpoint/resume for large groups

Usage:
    python create_branch_protection.py --group my-org/my-group
    python create_branch_protection.py --group 12345 --config config.yaml --dry-run
    python create_branch_protection.py --group my-org --tokens-file tokens.txt
"""

from __future__ import annotations

import argparse
import csv
import json
import logging
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import requests
import yaml

# Add workspace root to path for the shared gitlab_client module
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import gitlab_client as gl  # noqa: E402

log = logging.getLogger("branch_protection_create")

# ---------------------------------------------------------------------------
# Extended client (adds POST/DELETE with JSON body support)
# ---------------------------------------------------------------------------


class GitLabWriter:
    """Wraps the shared PooledClient to add POST/DELETE with JSON body."""

    def __init__(self, client: gl.PooledClient):
        self._client = client

    def post(
        self, path: str, *, json_body: dict | None = None, params: dict | None = None
    ) -> requests.Response:
        return self._request("POST", path, json_body=json_body, params=params)

    def patch(
        self, path: str, *, json_body: dict | None = None, params: dict | None = None
    ) -> requests.Response:
        return self._request("PATCH", path, json_body=json_body, params=params)

    def delete(self, path: str) -> requests.Response:
        return self._request("DELETE", path)

    def get(self, path: str, *, params: dict | None = None) -> requests.Response:
        return self._client.get(path, params=params)

    def _request(
        self,
        method: str,
        path: str,
        *,
        json_body: dict | None = None,
        params: dict | None = None,
    ) -> requests.Response:
        url = self._client._absolute(path)
        last: requests.Response | None = None

        for attempt in range(self._client.max_retries):
            state = self._client.pool.acquire()
            sess = self._client._session_for(state)
            try:
                resp = sess.request(
                    method,
                    url,
                    json=json_body,
                    params=params,
                    timeout=self._client.timeout,
                )
            except requests.exceptions.RequestException as exc:
                self._client.pool.release(state)
                wait = min(2**attempt, 30)
                log.warning(
                    "[%s] network error on %s %s (attempt %d): %s; retry in %ds",
                    state.masked, method, url, attempt + 1, exc, wait,
                )
                time.sleep(wait)
                continue

            self._client.pool.update_from_response(state, resp)
            self._client.pool.release(state)

            if resp.status_code == 429:
                wait = gl._parse_retry_after(resp.headers.get("Retry-After"))
                log.warning("[%s] 429 on %s — sleeping %.1fs", state.masked, url, wait)
                with state.lock:
                    state.reset_at = max(state.reset_at, time.time() + wait)
                    state.remaining = 0
                time.sleep(wait)
                last = resp
                continue

            if 500 <= resp.status_code < 600:
                wait = min(2**attempt, 30)
                log.warning(
                    "[%s] %d on %s — retry in %ds",
                    state.masked, resp.status_code, url, wait,
                )
                time.sleep(wait)
                last = resp
                continue

            return resp

        if last is not None:
            return last
        raise RuntimeError(f"All retries exhausted for {method} {url}")


# ---------------------------------------------------------------------------
# Core logic
# ---------------------------------------------------------------------------


def load_config(config_path: str | Path) -> dict:
    """Load the YAML configuration file."""
    with open(config_path, "r", encoding="utf-8") as f:
        return yaml.safe_load(f)


def get_existing_protections(writer: GitLabWriter, project_id: int) -> set[str]:
    """Return set of already-protected branch names for a project."""
    protected: set[str] = set()
    page = 1
    while True:
        resp = writer.get(
            f"/projects/{project_id}/protected_branches",
            params={"per_page": 100, "page": page},
        )
        if resp.status_code != 200:
            log.warning(
                "Failed to list protected branches for project %d: %d %s",
                project_id, resp.status_code, resp.text[:200],
            )
            break
        data = resp.json()
        if not data:
            break
        for branch in data:
            protected.add(branch["name"])
        page += 1
    return protected


def branch_exists(writer: GitLabWriter, project_id: int, branch_name: str) -> bool:
    """Check if a branch exists (for non-wildcard patterns)."""
    if "*" in branch_name:
        return True  # wildcards are always valid
    from urllib.parse import quote as url_quote
    encoded = url_quote(branch_name, safe="")
    resp = writer.get(f"/projects/{project_id}/repository/branches/{encoded}")
    return resp.status_code == 200


def create_branch(
    writer: GitLabWriter,
    project_id: int,
    branch_name: str,
    ref: str = "main",
    *,
    dry_run: bool = False,
) -> dict[str, Any]:
    """
    Create a branch in a project from the given ref (default branch).

    Uses POST /projects/:id/repository/branches
    """
    if "*" in branch_name:
        # Cannot create wildcard branches
        return {
            "project_id": project_id,
            "branch": branch_name,
            "action": "create_branch",
            "status": "skipped_wildcard",
        }

    if dry_run:
        log.info(
            "[DRY RUN] Would create branch '%s' from '%s' on project %d",
            branch_name, ref, project_id,
        )
        return {
            "project_id": project_id,
            "branch": branch_name,
            "action": "create_branch",
            "status": "dry_run",
        }

    resp = writer.post(
        f"/projects/{project_id}/repository/branches",
        json_body={"branch": branch_name, "ref": ref},
    )

    if resp.status_code in (200, 201):
        log.info("Created branch '%s' on project %d", branch_name, project_id)
        return {
            "project_id": project_id,
            "branch": branch_name,
            "action": "create_branch",
            "status": "created",
        }
    elif resp.status_code == 400 and "already exists" in resp.text.lower():
        log.debug("Branch '%s' already exists on project %d", branch_name, project_id)
        return {
            "project_id": project_id,
            "branch": branch_name,
            "action": "create_branch",
            "status": "already_exists",
        }
    else:
        log.error(
            "Failed to create branch '%s' on project %d: %d %s",
            branch_name, project_id, resp.status_code, resp.text[:300],
        )
        return {
            "project_id": project_id,
            "branch": branch_name,
            "action": "create_branch",
            "status": "error",
            "http_status": resp.status_code,
            "error": resp.text[:500],
        }


def protect_branch(
    writer: GitLabWriter,
    project_id: int,
    rule: dict,
    *,
    dry_run: bool = False,
) -> dict[str, Any]:
    """
    Apply a single branch protection rule to a project.

    Supports ALL GitLab protected branch API parameters:
      - push_access_level / merge_access_level / unprotect_access_level (simple)
      - allowed_to_push / allowed_to_merge / allowed_to_unprotect (Premium arrays)
      - allow_force_push
      - code_owner_approval_required (Premium/Ultimate)

    Returns a result dict with status info.
    """
    branch_name = rule["name"]
    payload: dict[str, Any] = {"name": branch_name}

    # --- Simple access level fields (Free tier) ---
    # Only include if granular arrays are NOT provided (they override these)
    if "allowed_to_push" not in rule:
        payload["push_access_level"] = rule.get("push_access_level", 40)
    if "allowed_to_merge" not in rule:
        payload["merge_access_level"] = rule.get("merge_access_level", 40)
    if "allowed_to_unprotect" not in rule:
        if "unprotect_access_level" in rule:
            payload["unprotect_access_level"] = rule["unprotect_access_level"]

    # --- Boolean fields ---
    payload["allow_force_push"] = rule.get("allow_force_push", False)
    if "code_owner_approval_required" in rule:
        payload["code_owner_approval_required"] = rule["code_owner_approval_required"]

    # --- Premium/Ultimate: Granular access arrays ---
    # allowed_to_push: [{access_level: int}, {user_id: int}, {group_id: int}, {deploy_key_id: int}]
    if "allowed_to_push" in rule:
        payload["allowed_to_push"] = rule["allowed_to_push"]

    # allowed_to_merge: [{access_level: int}, {user_id: int}, {group_id: int}]
    if "allowed_to_merge" in rule:
        payload["allowed_to_merge"] = rule["allowed_to_merge"]

    # allowed_to_unprotect: [{access_level: int}, {user_id: int}, {group_id: int}]
    if "allowed_to_unprotect" in rule:
        payload["allowed_to_unprotect"] = rule["allowed_to_unprotect"]

    if dry_run:
        log.info(
            "[DRY RUN] Would protect branch '%s' on project %d with: %s",
            branch_name, project_id, json.dumps(payload, default=str),
        )
        return {
            "project_id": project_id,
            "branch": branch_name,
            "status": "dry_run",
            "payload": payload,
        }

    resp = writer.post(
        f"/projects/{project_id}/protected_branches",
        json_body=payload,
    )

    if resp.status_code in (200, 201):
        log.info(
            "Protected branch '%s' on project %d", branch_name, project_id,
        )
        return {
            "project_id": project_id,
            "branch": branch_name,
            "status": "created",
            "response": resp.json(),
        }
    elif resp.status_code == 409:
        log.info(
            "Branch '%s' already protected on project %d (409 Conflict)",
            branch_name, project_id,
        )
        return {
            "project_id": project_id,
            "branch": branch_name,
            "status": "already_protected",
        }
    else:
        log.error(
            "Failed to protect branch '%s' on project %d: %d %s",
            branch_name, project_id, resp.status_code, resp.text[:300],
        )
        return {
            "project_id": project_id,
            "branch": branch_name,
            "status": "error",
            "http_status": resp.status_code,
            "error": resp.text[:500],
        }


def update_branch_protection(
    writer: GitLabWriter,
    project_id: int,
    rule: dict,
    *,
    dry_run: bool = False,
) -> dict[str, Any]:
    """
    Update an existing branch protection rule using PATCH.

    Supports ALL GitLab PATCH parameters:
      - allow_force_push
      - code_owner_approval_required
      - allowed_to_push (array with access_level/user_id/group_id/deploy_key_id)
      - allowed_to_merge (array with access_level/user_id/group_id)
      - allowed_to_unprotect (array with access_level/user_id/group_id)
    """
    from urllib.parse import quote as url_quote

    branch_name = rule["name"]
    encoded_name = url_quote(branch_name, safe="")
    payload: dict[str, Any] = {}

    # Boolean fields
    if "allow_force_push" in rule:
        payload["allow_force_push"] = rule["allow_force_push"]
    if "code_owner_approval_required" in rule:
        payload["code_owner_approval_required"] = rule["code_owner_approval_required"]

    # Granular arrays (Premium/Ultimate)
    if "allowed_to_push" in rule:
        payload["allowed_to_push"] = rule["allowed_to_push"]
    if "allowed_to_merge" in rule:
        payload["allowed_to_merge"] = rule["allowed_to_merge"]
    if "allowed_to_unprotect" in rule:
        payload["allowed_to_unprotect"] = rule["allowed_to_unprotect"]

    if not payload:
        return {
            "project_id": project_id,
            "branch": branch_name,
            "status": "skipped_no_update_fields",
        }

    if dry_run:
        log.info(
            "[DRY RUN] Would update branch '%s' on project %d with: %s",
            branch_name, project_id, json.dumps(payload, default=str),
        )
        return {
            "project_id": project_id,
            "branch": branch_name,
            "status": "dry_run_update",
            "payload": payload,
        }

    resp = writer.patch(
        f"/projects/{project_id}/protected_branches/{encoded_name}",
        json_body=payload,
    )

    if resp.status_code == 200:
        log.info(
            "Updated protection for branch '%s' on project %d",
            branch_name, project_id,
        )
        return {
            "project_id": project_id,
            "branch": branch_name,
            "status": "updated",
            "response": resp.json(),
        }
    else:
        log.error(
            "Failed to update branch '%s' on project %d: %d %s",
            branch_name, project_id, resp.status_code, resp.text[:300],
        )
        return {
            "project_id": project_id,
            "branch": branch_name,
            "status": "error",
            "http_status": resp.status_code,
            "error": resp.text[:500],
        }


def process_project(
    writer: GitLabWriter,
    project: dict,
    rules: list[dict],
    options: dict,
) -> list[dict[str, Any]]:
    """Apply all branch protection rules to a single project."""
    project_id = project["id"]
    project_path = project.get("path_with_namespace", str(project_id))
    default_branch = project.get("default_branch") or "main"
    dry_run = options.get("dry_run", False)
    skip_existing = options.get("skip_existing", True)
    update_existing = options.get("update_existing", False)
    protect_missing = options.get("protect_missing_branches", False)
    create_branches = options.get("create_branches", False)

    log.info("Processing project: %s (id=%d)", project_path, project_id)

    # Get existing protections
    existing = get_existing_protections(writer, project_id)
    results: list[dict[str, Any]] = []

    for rule in rules:
        branch_name = rule["name"]

        # Handle already-protected branches
        if branch_name in existing:
            if update_existing:
                # Update the existing protection with new settings
                result = update_branch_protection(
                    writer, project_id, rule, dry_run=dry_run
                )
                result["project_path"] = project_path
                results.append(result)
            elif skip_existing:
                log.info(
                    "Skipping '%s' on %s — already protected",
                    branch_name, project_path,
                )
                results.append({
                    "project_id": project_id,
                    "project_path": project_path,
                    "branch": branch_name,
                    "status": "skipped_existing",
                })
            continue

        # Check if branch exists
        if not branch_exists(writer, project_id, branch_name):
            if create_branches:
                # Create the branch from the project's default branch
                create_result = create_branch(
                    writer, project_id, branch_name, ref=default_branch, dry_run=dry_run
                )
                create_result["project_path"] = project_path
                results.append(create_result)
                if create_result["status"] == "error":
                    continue  # Skip protection if branch creation failed
            elif not protect_missing:
                log.info(
                    "Skipping '%s' on %s — branch does not exist",
                    branch_name, project_path,
                )
                results.append({
                    "project_id": project_id,
                    "project_path": project_path,
                    "branch": branch_name,
                    "status": "skipped_missing",
                })
                continue

        result = protect_branch(writer, project_id, rule, dry_run=dry_run)
        result["project_path"] = project_path
        results.append(result)

    return results


# ---------------------------------------------------------------------------
# Report
# ---------------------------------------------------------------------------


def write_report(results: list[dict], output_dir: Path) -> None:
    """Write results to JSON and CSV reports."""
    output_dir.mkdir(parents=True, exist_ok=True)
    ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")

    # JSON report
    json_path = output_dir / f"branch_protection_report_{ts}.json"
    with open(json_path, "w", encoding="utf-8") as f:
        json.dump(results, f, indent=2, default=str)
    log.info("JSON report: %s", json_path)

    # CSV report
    csv_path = output_dir / f"branch_protection_report_{ts}.csv"
    if results:
        fieldnames = ["project_id", "project_path", "branch", "status", "http_status", "error"]
        with open(csv_path, "w", encoding="utf-8", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=fieldnames, extrasaction="ignore")
            writer.writeheader()
            writer.writerows(results)
        log.info("CSV report: %s", csv_path)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Create branch protection rules for all projects in a GitLab group.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument(
        "--group", required=True,
        help="GitLab group ID or full path (e.g., 'my-org/my-team')",
    )
    p.add_argument(
        "--config", default=str(Path(__file__).parent / "config.yaml"),
        help="Path to YAML config file (default: config.yaml in script dir)",
    )
    p.add_argument(
        "--gitlab-url", default="https://gitlab.com",
        help="GitLab instance URL (default: https://gitlab.com)",
    )
    p.add_argument(
        "--tokens", default=None,
        help="Comma-separated GitLab PATs (or use GITLAB_TOKEN / GITLAB_TOKENS env)",
    )
    p.add_argument(
        "--tokens-file", default=None,
        help="File with one token per line",
    )
    p.add_argument(
        "--dry-run", action="store_true",
        help="Preview changes without applying them",
    )
    p.add_argument(
        "--output-dir", default=str(Path(__file__).parent / "reports"),
        help="Directory for output reports",
    )
    p.add_argument(
        "--workers", type=int, default=None,
        help="Number of parallel workers (overrides config)",
    )
    p.add_argument(
        "--include-archived", action="store_true",
        help="Include archived projects",
    )
    p.add_argument(
        "--update-existing", action="store_true",
        help="Update (PATCH) branches that are already protected instead of skipping them",
    )
    p.add_argument(
        "--verbose", "-v", action="store_true",
        help="Enable debug logging",
    )
    return p.parse_args()


def main() -> None:
    args = parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )

    # Load config
    config = load_config(args.config)
    rules = config.get("rules", [])
    options = config.get("options", {})

    if not rules:
        log.error("No rules defined in config file: %s", args.config)
        sys.exit(1)

    # CLI overrides
    if args.dry_run:
        options["dry_run"] = True
    if args.update_existing:
        options["update_existing"] = True
    if args.workers:
        options["workers"] = args.workers

    workers = options.get("workers", 4)
    dry_run = options.get("dry_run", False)

    if dry_run:
        log.info("*** DRY RUN MODE — no changes will be made ***")

    # Load tokens
    tokens = gl.load_tokens(
        args_tokens=args.tokens,
        tokens_file=args.tokens_file,
    )
    if not tokens:
        log.error(
            "No GitLab tokens found. Use --tokens, --tokens-file, "
            "or set GITLAB_TOKEN / GITLAB_TOKENS env variable."
        )
        sys.exit(1)

    log.info("Loaded %d token(s)", len(tokens))

    # Build client
    pool = gl.TokenPool(tokens)
    client = gl.PooledClient(args.gitlab_url, pool)
    writer = GitLabWriter(client)

    # Fetch projects in group
    log.info("Fetching projects in group: %s", args.group)
    archived_filter = None if args.include_archived else "false"
    projects = list(
        gl.list_group_projects(client, args.group, archived=archived_filter)
    )
    log.info("Found %d projects", len(projects))

    if not projects:
        log.warning("No projects found in group '%s'", args.group)
        sys.exit(0)

    # Process projects in parallel
    all_results: list[dict] = []

    with ThreadPoolExecutor(max_workers=workers) as executor:
        futures = {
            executor.submit(process_project, writer, proj, rules, options): proj
            for proj in projects
        }
        for future in as_completed(futures):
            proj = futures[future]
            try:
                results = future.result()
                all_results.extend(results)
            except Exception as exc:
                log.error(
                    "Error processing project %s: %s",
                    proj.get("path_with_namespace", proj["id"]),
                    exc,
                )
                all_results.append({
                    "project_id": proj["id"],
                    "project_path": proj.get("path_with_namespace", ""),
                    "branch": "*",
                    "status": "error",
                    "error": str(exc),
                })

    # Summary
    created = sum(1 for r in all_results if r.get("status") == "created")
    skipped = sum(1 for r in all_results if "skipped" in r.get("status", ""))
    errors = sum(1 for r in all_results if r.get("status") == "error")
    dry_runs = sum(1 for r in all_results if r.get("status") == "dry_run")

    log.info("=" * 60)
    log.info("SUMMARY")
    log.info("  Projects processed: %d", len(projects))
    log.info("  Rules created:      %d", created)
    log.info("  Skipped:            %d", skipped)
    log.info("  Errors:             %d", errors)
    if dry_run:
        log.info("  Dry-run previewed:  %d", dry_runs)
    log.info("=" * 60)

    # Write report
    write_report(all_results, Path(args.output_dir))


if __name__ == "__main__":
    main()
