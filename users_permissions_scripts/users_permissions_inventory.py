#!/usr/bin/env python3
"""
GitLab-to-GitHub User & Permission Migration Assessment Tool
=============================================================

Production-ready tool that performs a comprehensive inventory of GitLab
users, groups, projects, and permissions — then calculates effective
permissions (with inheritance) and maps them to GitHub equivalents.

Enterprise-scale support:
  - 5,000+ groups
  - 10,000+ projects
  - 50,000+ users
  - Unlimited subgroup nesting depth

Features:
  - Recursive group/subgroup discovery (unlimited depth)
  - Effective permission calculation with inheritance resolution
  - Highest-privilege conflict resolution
  - GitLab → GitHub role mapping
  - Pagination with configurable page size
  - Retry logic with exponential backoff
  - Rate-limit handling (429 + RateLimit headers)
  - Concurrent API calls via ThreadPoolExecutor
  - Structured logging (console + file)
  - Single Excel workbook output with 8 sheets

Usage:
    python users_permissions_inventory.py --group my-org --token glpat-xxx
    python users_permissions_inventory.py --group my-org --token glpat-xxx --url https://gitlab.example.com
    python users_permissions_inventory.py --group my-org --workers 8 --debug

Environment variables (CLI overrides take precedence):
    GITLAB_URL   - GitLab instance URL (default: https://gitlab.com)
    GITLAB_TOKEN - Personal Access Token with read_api scope
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
import time
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import quote

import requests

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

VERSION = "1.0.0"
DEFAULT_GITLAB_URL = "https://gitlab.com"
DEFAULT_EXCEL_FILENAME = "GitLab_Migration_Inventory.xlsx"
DEFAULT_LOG_FILENAME = "users_permissions_inventory.log"
DEFAULT_PAGE_SIZE = 100
DEFAULT_WORKERS = 4
MAX_RETRIES = 5
BACKOFF_BASE = 1.0
BACKOFF_MAX = 60.0

# GitLab access levels
GITLAB_ACCESS_LEVELS: dict[int, str] = {
    10: "Guest",
    20: "Reporter",
    30: "Developer",
    40: "Maintainer",
    50: "Owner",
}

# GitLab → GitHub role mapping
GITHUB_ROLE_MAP: dict[str, str] = {
    "Guest": "Read",
    "Reporter": "Triage",
    "Developer": "Write",
    "Maintainer": "Maintain",
    "Owner": "Admin",
}

# ---------------------------------------------------------------------------
# Logging setup
# ---------------------------------------------------------------------------

log = logging.getLogger("users_permissions")


def setup_logging(log_file: Path, debug: bool = False) -> None:
    level = logging.DEBUG if debug else logging.INFO
    fmt = "%(asctime)s [%(levelname)s] %(name)s: %(message)s"
    handlers: list[logging.Handler] = [
        logging.StreamHandler(sys.stdout),
        logging.FileHandler(str(log_file), mode="w", encoding="utf-8"),
    ]
    logging.basicConfig(level=level, format=fmt, handlers=handlers)


# ---------------------------------------------------------------------------
# HTTP Client with retry, backoff, rate-limit handling
# ---------------------------------------------------------------------------

class GitLabClient:
    """Thread-safe HTTP client for the GitLab REST API."""

    def __init__(self, base_url: str, token: str, page_size: int = DEFAULT_PAGE_SIZE):
        self.base_url = base_url.rstrip("/")
        self.token = token
        self.page_size = page_size
        self._session = requests.Session()
        self._session.headers.update({
            "PRIVATE-TOKEN": token,
            "Accept": "application/json",
        })
        self._rate_lock = threading.Lock()
        self._rate_remaining: int | None = None
        self._rate_reset: float = 0.0

    def _update_rate_info(self, resp: requests.Response) -> None:
        remaining = resp.headers.get("RateLimit-Remaining")
        reset = resp.headers.get("RateLimit-Reset")
        with self._rate_lock:
            if remaining is not None:
                self._rate_remaining = int(remaining)
            if reset is not None:
                self._rate_reset = float(reset)

    def _wait_for_rate_limit(self) -> None:
        with self._rate_lock:
            if self._rate_remaining is not None and self._rate_remaining < 10:
                wait = max(self._rate_reset - time.time(), 0.0) + 1.0
                if wait > 0:
                    log.warning("Rate limit near exhaustion, sleeping %.1fs", wait)
                    time.sleep(wait)

    def get(self, endpoint: str, params: dict[str, Any] | None = None) -> requests.Response:
        """Single GET with retry + backoff."""
        url = f"{self.base_url}/api/v4/{endpoint.lstrip('/')}"
        self._wait_for_rate_limit()

        for attempt in range(1, MAX_RETRIES + 1):
            try:
                resp = self._session.get(url, params=params, timeout=30)
                self._update_rate_info(resp)

                if resp.status_code == 200:
                    return resp
                if resp.status_code == 429:
                    retry_after = resp.headers.get("Retry-After", "5")
                    wait = float(retry_after) if retry_after.isdigit() else 5.0
                    log.warning("429 rate-limited, retrying in %.1fs (attempt %d)", wait, attempt)
                    time.sleep(wait)
                    continue
                if resp.status_code >= 500:
                    backoff = min(BACKOFF_BASE * (2 ** (attempt - 1)), BACKOFF_MAX)
                    log.warning("Server error %d, retrying in %.1fs (attempt %d)", resp.status_code, backoff, attempt)
                    time.sleep(backoff)
                    continue
                # 4xx (not 429) — no retry
                resp.raise_for_status()
            except requests.exceptions.Timeout:
                backoff = min(BACKOFF_BASE * (2 ** (attempt - 1)), BACKOFF_MAX)
                log.warning("Timeout, retrying in %.1fs (attempt %d)", backoff, attempt)
                time.sleep(backoff)
            except requests.exceptions.ConnectionError:
                backoff = min(BACKOFF_BASE * (2 ** (attempt - 1)), BACKOFF_MAX)
                log.warning("Connection error, retrying in %.1fs (attempt %d)", backoff, attempt)
                time.sleep(backoff)

        raise RuntimeError(f"Failed after {MAX_RETRIES} retries: GET {url}")

    def get_all(self, endpoint: str, params: dict[str, Any] | None = None) -> list[dict[str, Any]]:
        """Paginated GET returning all results."""
        results: list[dict[str, Any]] = []
        p = dict(params) if params else {}
        p.setdefault("per_page", self.page_size)
        p.setdefault("page", 1)

        while True:
            resp = self.get(endpoint, params=p)
            data = resp.json()
            if not data:
                break
            results.extend(data)
            next_page = resp.headers.get("X-Next-Page", "").strip()
            if not next_page:
                break
            p["page"] = int(next_page)

        return results


# ---------------------------------------------------------------------------
# Data models
# ---------------------------------------------------------------------------

@dataclass
class GroupInfo:
    id: int
    name: str
    full_path: str
    parent_id: int | None
    depth: int
    visibility: str


@dataclass
class ProjectInfo:
    id: int
    name: str
    path_with_namespace: str
    group_id: int | None
    visibility: str
    archived: bool
    last_activity_at: str


@dataclass
class UserInfo:
    id: int
    username: str
    name: str
    email: str
    state: str
    last_activity_on: str


@dataclass
class Membership:
    user_id: int
    username: str
    name: str
    access_level: int
    access_level_name: str
    source_id: int  # group or project ID
    source_type: str  # "group" or "project"
    expires_at: str | None


@dataclass
class EffectivePermission:
    user_id: int
    username: str
    name: str
    target_id: int
    target_type: str  # "group" or "project"
    target_path: str
    effective_access_level: int
    effective_role: str
    github_role: str
    inherited_from: str  # "direct" or group path


# ---------------------------------------------------------------------------
# Discovery engine
# ---------------------------------------------------------------------------

class MigrationAssessment:
    """Orchestrates the full migration assessment."""

    def __init__(self, client: GitLabClient, root_group: str, workers: int = DEFAULT_WORKERS):
        self.client = client
        self.root_group = root_group
        self.workers = workers

        # Collected data
        self.groups: list[GroupInfo] = []
        self.projects: list[ProjectInfo] = []
        self.users: dict[int, UserInfo] = {}
        self.group_memberships: list[Membership] = []
        self.project_memberships: list[Membership] = []
        self.effective_group_permissions: list[EffectivePermission] = []
        self.effective_project_permissions: list[EffectivePermission] = []

        # Internal lookup structures
        self._group_by_id: dict[int, GroupInfo] = {}
        self._children: dict[int, list[int]] = {}  # parent_id → child group IDs
        self._group_members: dict[int, dict[int, int]] = {}  # group_id → {user_id: access_level}
        self._project_members: dict[int, dict[int, int]] = {}  # project_id → {user_id: access_level}

    # ------------------------------------------------------------------
    # Group discovery (recursive, unlimited depth)
    # ------------------------------------------------------------------

    def discover_groups(self) -> None:
        """Discover root group and all descendant subgroups."""
        log.info("Discovering groups starting from '%s'...", self.root_group)
        encoded = quote(self.root_group, safe="") if not self.root_group.isdigit() else self.root_group
        root_data = self.client.get(f"groups/{encoded}").json()

        root = GroupInfo(
            id=root_data["id"],
            name=root_data["name"],
            full_path=root_data["full_path"],
            parent_id=root_data.get("parent_id"),
            depth=0,
            visibility=root_data.get("visibility", "private"),
        )
        self.groups.append(root)
        self._group_by_id[root.id] = root

        # Fetch all descendant subgroups
        descendants = self.client.get_all(
            f"groups/{root.id}/descendant_groups",
            params={"all_available": "false"},
        )
        log.info("Found %d descendant groups", len(descendants))

        for g in descendants:
            group = GroupInfo(
                id=g["id"],
                name=g["name"],
                full_path=g["full_path"],
                parent_id=g.get("parent_id"),
                depth=0,  # calculated below
                visibility=g.get("visibility", "private"),
            )
            self.groups.append(group)
            self._group_by_id[group.id] = group

        # Build parent→children map and calculate depths
        for g in self.groups:
            if g.parent_id is not None:
                self._children.setdefault(g.parent_id, []).append(g.id)

        self._calculate_depths(root.id, 0)
        log.info("Total groups discovered: %d (max depth: %d)",
                 len(self.groups),
                 max(g.depth for g in self.groups) if self.groups else 0)

    def _calculate_depths(self, group_id: int, depth: int) -> None:
        """BFS-style depth calculation."""
        if group_id in self._group_by_id:
            self._group_by_id[group_id].depth = depth
        for child_id in self._children.get(group_id, []):
            self._calculate_depths(child_id, depth + 1)

    # ------------------------------------------------------------------
    # Project discovery
    # ------------------------------------------------------------------

    def discover_projects(self) -> None:
        """Discover all projects across all groups concurrently."""
        log.info("Discovering projects across %d groups...", len(self.groups))

        def fetch_projects(group: GroupInfo) -> list[ProjectInfo]:
            raw = self.client.get_all(
                f"groups/{group.id}/projects",
                params={"include_subgroups": "false", "archived": "true", "with_shared": "false"},
            )
            results = []
            for p in raw:
                proj = ProjectInfo(
                    id=p["id"],
                    name=p["name"],
                    path_with_namespace=p["path_with_namespace"],
                    group_id=group.id,
                    visibility=p.get("visibility", "private"),
                    archived=p.get("archived", False),
                    last_activity_at=p.get("last_activity_at", ""),
                )
                results.append(proj)
            return results

        with ThreadPoolExecutor(max_workers=self.workers) as pool:
            futures = {pool.submit(fetch_projects, g): g for g in self.groups}
            for future in as_completed(futures):
                group = futures[future]
                try:
                    projects = future.result()
                    self.projects.extend(projects)
                except Exception as e:
                    log.error("Error fetching projects for group %s: %s", group.full_path, e)

        log.info("Total projects discovered: %d", len(self.projects))

    # ------------------------------------------------------------------
    # Member collection
    # ------------------------------------------------------------------

    def collect_group_members(self) -> None:
        """Collect members for all groups concurrently."""
        log.info("Collecting group members...")

        def fetch_members(group: GroupInfo) -> list[Membership]:
            raw = self.client.get_all(f"groups/{group.id}/members")
            members = []
            for m in raw:
                access_level = m.get("access_level", 0)
                membership = Membership(
                    user_id=m["id"],
                    username=m.get("username", ""),
                    name=m.get("name", ""),
                    access_level=access_level,
                    access_level_name=GITLAB_ACCESS_LEVELS.get(access_level, f"Unknown({access_level})"),
                    source_id=group.id,
                    source_type="group",
                    expires_at=m.get("expires_at"),
                )
                members.append(membership)
                # Track user info
                self._register_user(m)
            return members

        with ThreadPoolExecutor(max_workers=self.workers) as pool:
            futures = {pool.submit(fetch_members, g): g for g in self.groups}
            for future in as_completed(futures):
                group = futures[future]
                try:
                    members = future.result()
                    self.group_memberships.extend(members)
                    self._group_members[group.id] = {m.user_id: m.access_level for m in members}
                except Exception as e:
                    log.error("Error fetching members for group %s: %s", group.full_path, e)

        log.info("Total group memberships: %d", len(self.group_memberships))

    def collect_project_members(self) -> None:
        """Collect members for all projects concurrently."""
        log.info("Collecting project members...")

        def fetch_members(project: ProjectInfo) -> list[Membership]:
            raw = self.client.get_all(f"projects/{project.id}/members")
            members = []
            for m in raw:
                access_level = m.get("access_level", 0)
                membership = Membership(
                    user_id=m["id"],
                    username=m.get("username", ""),
                    name=m.get("name", ""),
                    access_level=access_level,
                    access_level_name=GITLAB_ACCESS_LEVELS.get(access_level, f"Unknown({access_level})"),
                    source_id=project.id,
                    source_type="project",
                    expires_at=m.get("expires_at"),
                )
                members.append(membership)
                self._register_user(m)
            return members

        with ThreadPoolExecutor(max_workers=self.workers) as pool:
            futures = {pool.submit(fetch_members, p): p for p in self.projects}
            for future in as_completed(futures):
                project = futures[future]
                try:
                    members = future.result()
                    self.project_memberships.extend(members)
                    self._project_members[project.id] = {m.user_id: m.access_level for m in members}
                except Exception as e:
                    log.error("Error fetching members for project %s: %s", project.path_with_namespace, e)

        log.info("Total project memberships: %d", len(self.project_memberships))

    def _register_user(self, member_data: dict[str, Any]) -> None:
        """Register user from member response data (thread-safe via dict)."""
        uid = member_data["id"]
        if uid not in self.users:
            self.users[uid] = UserInfo(
                id=uid,
                username=member_data.get("username", ""),
                name=member_data.get("name", ""),
                email=member_data.get("email", ""),
                state=member_data.get("state", "unknown"),
                last_activity_on=member_data.get("last_activity_on", ""),
            )

    # ------------------------------------------------------------------
    # Effective permission calculation
    # ------------------------------------------------------------------

    def calculate_effective_permissions(self) -> None:
        """Calculate effective permissions with inheritance resolution."""
        log.info("Calculating effective permissions...")
        self._calculate_effective_group_permissions()
        self._calculate_effective_project_permissions()
        log.info("Effective group permissions: %d, project permissions: %d",
                 len(self.effective_group_permissions),
                 len(self.effective_project_permissions))

    def _get_ancestor_chain(self, group_id: int) -> list[int]:
        """Get ordered list of ancestor group IDs from root to group (inclusive)."""
        chain: list[int] = []
        current_id: int | None = group_id
        while current_id is not None and current_id in self._group_by_id:
            chain.append(current_id)
            current_id = self._group_by_id[current_id].parent_id
        chain.reverse()
        return chain

    def _calculate_effective_group_permissions(self) -> None:
        """For each group, calculate each user's effective permission."""
        # For every group, walk up the ancestor chain and collect
        # the highest access level a user has via direct or inherited membership.
        for group in self.groups:
            ancestor_chain = self._get_ancestor_chain(group.id)
            user_max: dict[int, tuple[int, str]] = {}  # user_id → (max_level, source_path)

            for ancestor_id in ancestor_chain:
                members = self._group_members.get(ancestor_id, {})
                ancestor_path = self._group_by_id[ancestor_id].full_path if ancestor_id in self._group_by_id else "unknown"
                for uid, level in members.items():
                    current = user_max.get(uid)
                    if current is None or level > current[0]:
                        source = "direct" if ancestor_id == group.id else ancestor_path
                        user_max[uid] = (level, source)

            for uid, (level, source) in user_max.items():
                user = self.users.get(uid)
                if user is None:
                    continue
                role = GITLAB_ACCESS_LEVELS.get(level, f"Unknown({level})")
                self.effective_group_permissions.append(EffectivePermission(
                    user_id=uid,
                    username=user.username,
                    name=user.name,
                    target_id=group.id,
                    target_type="group",
                    target_path=group.full_path,
                    effective_access_level=level,
                    effective_role=role,
                    github_role=GITHUB_ROLE_MAP.get(role, "Read"),
                    inherited_from=source,
                ))

    def _calculate_effective_project_permissions(self) -> None:
        """For each project, calculate each user's effective permission."""
        for project in self.projects:
            user_max: dict[int, tuple[int, str]] = {}  # user_id → (max_level, source)

            # 1. Direct project membership
            direct_members = self._project_members.get(project.id, {})
            for uid, level in direct_members.items():
                user_max[uid] = (level, "direct")

            # 2. Inherited from parent group chain
            if project.group_id and project.group_id in self._group_by_id:
                ancestor_chain = self._get_ancestor_chain(project.group_id)
                for ancestor_id in ancestor_chain:
                    members = self._group_members.get(ancestor_id, {})
                    ancestor_path = self._group_by_id[ancestor_id].full_path if ancestor_id in self._group_by_id else "unknown"
                    for uid, level in members.items():
                        current = user_max.get(uid)
                        if current is None or level > current[0]:
                            user_max[uid] = (level, ancestor_path)

            for uid, (level, source) in user_max.items():
                user = self.users.get(uid)
                if user is None:
                    continue
                role = GITLAB_ACCESS_LEVELS.get(level, f"Unknown({level})")
                self.effective_project_permissions.append(EffectivePermission(
                    user_id=uid,
                    username=user.username,
                    name=user.name,
                    target_id=project.id,
                    target_type="project",
                    target_path=project.path_with_namespace,
                    effective_access_level=level,
                    effective_role=role,
                    github_role=GITHUB_ROLE_MAP.get(role, "Read"),
                    inherited_from=source,
                ))

    # ------------------------------------------------------------------
    # Excel report generation
    # ------------------------------------------------------------------

    def generate_report(self, output_path: Path) -> None:
        """Generate the Excel workbook with all sheets."""
        try:
            import openpyxl
            from openpyxl.styles import Font, PatternFill, Alignment
            from openpyxl.utils import get_column_letter
        except ImportError:
            log.error("openpyxl is required: pip install openpyxl")
            sys.exit(1)

        log.info("Generating report: %s", output_path)
        wb = openpyxl.Workbook()

        # Style constants
        header_font = Font(bold=True, color="FFFFFF")
        header_fill = PatternFill(start_color="4472C4", end_color="4472C4", fill_type="solid")

        def write_sheet(ws: Any, headers: list[str], rows: list[list[Any]]) -> None:
            ws.append(headers)
            for cell in ws[1]:
                cell.font = header_font
                cell.fill = header_fill
                cell.alignment = Alignment(horizontal="center")
            for row in rows:
                ws.append(row)
            # Auto-width
            for col_idx, _ in enumerate(headers, 1):
                col_letter = get_column_letter(col_idx)
                max_len = max(
                    len(str(headers[col_idx - 1])),
                    *(len(str(row[col_idx - 1])) for row in rows[:100]) if rows else [0],
                )
                ws.column_dimensions[col_letter].width = min(max_len + 2, 50)

        # 1. Groups sheet
        ws = wb.active
        ws.title = "Groups"
        write_sheet(ws,
                    ["Group ID", "Parent Group ID", "Name", "Full Path", "Depth", "Visibility"],
                    [[g.id, g.parent_id or "", g.name, g.full_path, g.depth, g.visibility]
                     for g in sorted(self.groups, key=lambda x: x.full_path)])

        # 2. Users sheet
        ws = wb.create_sheet("Users")
        write_sheet(ws,
                    ["User ID", "Username", "Name", "Email", "State", "Last Activity"],
                    [[u.id, u.username, u.name, u.email, u.state, u.last_activity_on]
                     for u in sorted(self.users.values(), key=lambda x: x.username)])

        # 3. GroupMemberships sheet
        ws = wb.create_sheet("GroupMemberships")
        write_sheet(ws,
                    ["User ID", "Username", "Name", "Group ID", "Group Path", "Access Level", "Role", "Expires At"],
                    [[m.user_id, m.username, m.name, m.source_id,
                      self._group_by_id[m.source_id].full_path if m.source_id in self._group_by_id else "",
                      m.access_level, m.access_level_name, m.expires_at or ""]
                     for m in sorted(self.group_memberships, key=lambda x: (x.source_id, x.username))])

        # 4. Projects sheet
        ws = wb.create_sheet("Projects")
        write_sheet(ws,
                    ["Project ID", "Name", "Full Path", "Group ID", "Visibility", "Archived", "Last Activity"],
                    [[p.id, p.name, p.path_with_namespace, p.group_id or "", p.visibility,
                      p.archived, p.last_activity_at]
                     for p in sorted(self.projects, key=lambda x: x.path_with_namespace)])

        # 5. ProjectMemberships sheet
        ws = wb.create_sheet("ProjectMemberships")
        proj_lookup = {p.id: p.path_with_namespace for p in self.projects}
        write_sheet(ws,
                    ["User ID", "Username", "Name", "Project ID", "Project Path", "Access Level", "Role", "Expires At"],
                    [[m.user_id, m.username, m.name, m.source_id,
                      proj_lookup.get(m.source_id, ""),
                      m.access_level, m.access_level_name, m.expires_at or ""]
                     for m in sorted(self.project_memberships, key=lambda x: (x.source_id, x.username))])

        # 6. EffectiveGroupPermissions sheet
        ws = wb.create_sheet("EffectiveGroupPermissions")
        write_sheet(ws,
                    ["User ID", "Username", "Name", "Group ID", "Group Path",
                     "Effective Access Level", "Effective Role", "GitHub Role", "Inherited From"],
                    [[ep.user_id, ep.username, ep.name, ep.target_id, ep.target_path,
                      ep.effective_access_level, ep.effective_role, ep.github_role, ep.inherited_from]
                     for ep in sorted(self.effective_group_permissions, key=lambda x: (x.target_path, x.username))])

        # 7. EffectiveProjectPermissions sheet
        ws = wb.create_sheet("EffectiveProjectPermissions")
        write_sheet(ws,
                    ["User ID", "Username", "Name", "Project ID", "Project Path",
                     "Effective Access Level", "Effective Role", "GitHub Role", "Inherited From"],
                    [[ep.user_id, ep.username, ep.name, ep.target_id, ep.target_path,
                      ep.effective_access_level, ep.effective_role, ep.github_role, ep.inherited_from]
                     for ep in sorted(self.effective_project_permissions, key=lambda x: (x.target_path, x.username))])

        # 8. GitHubMigrationMapping sheet
        ws = wb.create_sheet("GitHubMigrationMapping")
        # Consolidate: for each user, show their highest effective role per project → GitHub role
        migration_rows: list[list[Any]] = []
        user_project_max: dict[tuple[int, int], EffectivePermission] = {}
        for ep in self.effective_project_permissions:
            key = (ep.user_id, ep.target_id)
            current = user_project_max.get(key)
            if current is None or ep.effective_access_level > current.effective_access_level:
                user_project_max[key] = ep

        for ep in sorted(user_project_max.values(), key=lambda x: (x.target_path, x.username)):
            migration_rows.append([
                ep.username,
                ep.name,
                ep.target_path,
                ep.effective_role,
                ep.effective_access_level,
                ep.github_role,
                ep.inherited_from,
            ])

        write_sheet(ws,
                    ["Username", "Name", "GitLab Project", "GitLab Role",
                     "Access Level", "GitHub Role", "Permission Source"],
                    migration_rows)

        wb.save(str(output_path))
        log.info("Report saved: %s", output_path)

    # ------------------------------------------------------------------
    # Summary & validation
    # ------------------------------------------------------------------

    def print_summary(self) -> None:
        """Print execution summary with validation checks."""
        separator = "=" * 60
        print(f"\n{separator}")
        print("  GitLab-to-GitHub Migration Assessment Summary")
        print(separator)
        print(f"  Groups discovered:           {len(self.groups):>8,}")
        print(f"  Projects discovered:         {len(self.projects):>8,}")
        print(f"  Unique users found:          {len(self.users):>8,}")
        print(f"  Group memberships:           {len(self.group_memberships):>8,}")
        print(f"  Project memberships:         {len(self.project_memberships):>8,}")
        print(f"  Effective group permissions: {len(self.effective_group_permissions):>8,}")
        print(f"  Effective project perms:     {len(self.effective_project_permissions):>8,}")
        print(separator)

        # Validation checks
        max_depth = max((g.depth for g in self.groups), default=0)
        print(f"\n  Validation Checks:")
        print(f"  - Max subgroup depth:        {max_depth}")
        print(f"  - Orphan groups (no parent): {sum(1 for g in self.groups if g.parent_id is None)}")
        print(f"  - Archived projects:         {sum(1 for p in self.projects if p.archived)}")
        print(f"  - Active users:              {sum(1 for u in self.users.values() if u.state == 'active')}")
        print(f"  - Blocked users:             {sum(1 for u in self.users.values() if u.state == 'blocked')}")

        # Role distribution
        role_counts: dict[str, int] = {}
        for ep in self.effective_project_permissions:
            role_counts[ep.github_role] = role_counts.get(ep.github_role, 0) + 1
        print(f"\n  GitHub Role Distribution (project-level):")
        for role in ["Read", "Triage", "Write", "Maintain", "Admin"]:
            count = role_counts.get(role, 0)
            print(f"    {role:<10}: {count:>8,}")

        # Migration recommendations
        print(f"\n  Migration Recommendations:")
        print(f"  - Total permission assignments to migrate: {len(self.effective_project_permissions):,}")
        if sum(1 for u in self.users.values() if u.state == "blocked") > 0:
            print(f"  - Review blocked users before migration (may need deactivation in GitHub)")
        if max_depth > 5:
            print(f"  - Deep nesting detected ({max_depth} levels). GitHub has flat org/team structure.")
            print(f"    Consider flattening permission hierarchy.")
        print(f"{separator}\n")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="GitLab-to-GitHub User & Permission Migration Assessment Tool",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  %(prog)s --group my-org --token glpat-xxxxxxxxxxxxxxxxxxxx
  %(prog)s --group 12345 --url https://gitlab.example.com --workers 8
  %(prog)s --group my-org --output inventory.xlsx --debug

Environment Variables:
  GITLAB_URL    GitLab instance URL (default: https://gitlab.com)
  GITLAB_TOKEN  Personal Access Token (read_api scope required)
""",
    )
    parser.add_argument("--group", "-g", required=True,
                        help="Root GitLab group path or numeric ID")
    parser.add_argument("--token", "-t",
                        default=os.environ.get("GITLAB_TOKEN", ""),
                        help="GitLab Personal Access Token (or set GITLAB_TOKEN env var)")
    parser.add_argument("--url", "-u",
                        default=os.environ.get("GITLAB_URL", DEFAULT_GITLAB_URL),
                        help=f"GitLab instance URL (default: {DEFAULT_GITLAB_URL})")
    parser.add_argument("--output", "-o",
                        default=DEFAULT_EXCEL_FILENAME,
                        help=f"Output Excel filename (default: {DEFAULT_EXCEL_FILENAME})")
    parser.add_argument("--workers", "-w", type=int, default=DEFAULT_WORKERS,
                        help=f"Concurrent API workers (default: {DEFAULT_WORKERS})")
    parser.add_argument("--page-size", type=int, default=DEFAULT_PAGE_SIZE,
                        help=f"API page size (default: {DEFAULT_PAGE_SIZE})")
    parser.add_argument("--debug", action="store_true",
                        help="Enable debug logging")
    parser.add_argument("--version", action="version", version=f"%(prog)s {VERSION}")
    return parser.parse_args()


def main() -> None:
    args = parse_args()

    if not args.token:
        print("ERROR: GitLab token is required. Use --token or set GITLAB_TOKEN env var.", file=sys.stderr)
        sys.exit(1)

    # Setup paths
    script_dir = Path(__file__).resolve().parent
    log_file = script_dir / DEFAULT_LOG_FILENAME
    output_path = script_dir / args.output

    setup_logging(log_file, args.debug)
    log.info("GitLab-to-GitHub User & Permission Migration Assessment v%s", VERSION)
    log.info("GitLab URL: %s", args.url)
    log.info("Root group: %s", args.group)
    log.info("Workers: %d, Page size: %d", args.workers, args.page_size)

    start_time = time.time()

    # Initialize client and assessment
    client = GitLabClient(args.url, args.token, args.page_size)
    assessment = MigrationAssessment(client, args.group, args.workers)

    # Execute pipeline
    assessment.discover_groups()
    assessment.discover_projects()
    assessment.collect_group_members()
    assessment.collect_project_members()
    assessment.calculate_effective_permissions()
    assessment.generate_report(output_path)
    assessment.print_summary()

    elapsed = time.time() - start_time
    log.info("Assessment completed in %.1f seconds", elapsed)
    print(f"  Completed in {elapsed:.1f}s. Report: {output_path}")


if __name__ == "__main__":
    main()
