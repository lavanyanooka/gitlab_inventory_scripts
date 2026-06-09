"""
GitLab project inventory (parallel, multi-token, rate-limit-aware).

For every project under a GitLab group (recursing all nested subgroups),
collect:
  - basic metadata (size, branch_count, file_count, last_activity, etc.)
  - commits (across all branches, unless --no-branch-walk)
  - exportable model counts (members, MRs, issues, hooks, tags, ...)
  - LFS detection
  - CI/CD pipeline detection
  - 100 MB / 2 GB / 6 GB size flags
  - subgroup hierarchy columns

Writes one row per project to `data/gitlab-stats.csv`.

Features
--------
* Multiple PATs in a round-robin pool with per-token rate-limit tracking
  (RateLimit-Remaining / RateLimit-Reset / Retry-After).
* Parallel project processing via ThreadPoolExecutor (N tokens x M workers).
* Resume: completed project IDs are checkpointed; reruns skip them.
* Backward-compatible: still reads GITLAB_TOKEN / GITLAB_GROUP env vars
  and `gl-migrate.conf` / `.token` config files.

Configuration priority
----------------------
  1. CLI args (--group, --tokens, --gitlab-url, ...)
  2. Environment (GITLAB_TOKEN, GITLAB_TOKENS, GITLAB_GROUP, GITLAB_URL)
  3. `gl-migrate.conf` (JSON or key=value)
  4. `.token` (JSON)

Usage
-----
    set GITLAB_TOKEN=glpat-xxx
    set GITLAB_GROUP=my-group
    python gitlab.py

    REM 3 PATs, 4 workers each = 12 concurrent projects
    set GITLAB_TOKENS=glpat-a,glpat-b,glpat-c
    python gitlab.py --workers-per-token 4

    REM Skip the slow per-branch commit + file walks
    python gitlab.py --no-branch-walk

    REM Resume after Ctrl+C / crash (default behaviour)
    python gitlab.py

Note about throughput
---------------------
GitLab rate limits are per authenticated user. Three PATs on the same user
share one budget. To actually get Nx throughput, each PAT must belong to a
distinct user (or service account).
"""

from __future__ import annotations

import argparse
import base64
import csv
import fnmatch
import json
import logging
import os
import sys
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from pathlib import Path
from urllib.parse import quote

# Import the shared client (file lives at the workspace root).
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from gitlab_client import (  # noqa: E402
    CheckpointStore,
    CsvSink,
    PooledClient,
    TokenPool,
    encode_group,
    get_group,
    list_group_projects,
    load_tokens,
)


# ---------------------------------------------------------------------------
# CLI parsing (done early so --help works without any side effects)
# ---------------------------------------------------------------------------

def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="GitLab project inventory (parallel, multi-token).",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument(
        "--group",
        help=(
            "Group full path or numeric ID "
            "(default: env GITLAB_GROUP / config file)."
        ),
    )
    p.add_argument(
        "--gitlab-url",
        help=(
            "GitLab base URL "
            "(default: env GITLAB_URL or https://gitlab.com)."
        ),
    )
    p.add_argument(
        "--token",
        help="Single PAT (default: env GITLAB_TOKEN).",
    )
    p.add_argument(
        "--tokens",
        help=(
            "Comma-separated PATs for round-robin pool "
            "(default: env GITLAB_TOKENS)."
        ),
    )
    p.add_argument(
        "--tokens-file",
        help="File with one PAT per line ('#' starts a comment).",
    )
    p.add_argument(
        "--workers-per-token",
        type=int,
        default=4,
        help="Concurrent worker threads per token (default 4).",
    )
    p.add_argument(
        "--rate-limit-floor",
        type=int,
        default=50,
        help=(
            "Pause a token when RateLimit-Remaining drops below this "
            "(default 50)."
        ),
    )
    p.add_argument(
        "--output",
        help="Output CSV path (default data/gitlab-stats.csv).",
    )
    p.add_argument(
        "--checkpoint",
        help=(
            "Resume file of completed project IDs "
            "(default data/.processed_projects)."
        ),
    )
    p.add_argument(
        "--no-resume",
        action="store_true",
        help="Ignore checkpoint and process every project.",
    )
    p.add_argument(
        "--no-branch-walk",
        action="store_true",
        help=(
            "Skip per-branch commit/file walks "
            "(huge speedup; loses cross-branch metrics)."
        ),
    )
    p.add_argument("--verbose", "-v", action="store_true")
    return p.parse_known_args(argv)[0]


_args = _parse_args()


def log(message: str) -> None:
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    print(f"[{timestamp}] {message}")


script_start_time = datetime.now()
log("Starting GitLab Project Details script")

script_dir = Path(__file__).parent
data_dir = script_dir / "data"
data_dir.mkdir(parents=True, exist_ok=True)


# ---------------------------------------------------------------------------
# Configuration: env + config files + CLI args
# ---------------------------------------------------------------------------

log("Loading configuration...")
GITLAB_TOKEN: str | None = os.environ.get("GITLAB_TOKEN")
GROUP_NAME: str | None = _args.group or os.environ.get("GITLAB_GROUP")
GITLAB_URL: str = (
    _args.gitlab_url
    or os.environ.get("GITLAB_URL", "https://gitlab.com")
)
GITHUB_TOKEN: str | None = os.environ.get("GITHUB_TOKEN")
PROJECT_LIST_FILE: str | None = None
MIGRATE_REPO_VALUES: list[str] = []


def _load_keyvalue_or_json(path: Path) -> dict:
    content = path.read_text(encoding="utf-8")
    if content.strip().startswith("{"):
        return json.loads(content)
    out: dict = {}
    for line in content.splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            key, value = line.split("=", 1)
            out[key.strip()] = value.strip().strip('"').strip("'")
    return out


_gl_migrate_config_file = script_dir / "gl-migrate.conf"
_token_file = script_dir / ".token"

if _gl_migrate_config_file.exists():
    try:
        log(f"Reading config: {_gl_migrate_config_file}")
        cfg = _load_keyvalue_or_json(_gl_migrate_config_file)
        GITLAB_TOKEN = (
            GITLAB_TOKEN
            or cfg.get("GITLAB_TOKEN")
            or cfg.get("GITLAB_API_PRIVATE_TOKEN")
            or cfg.get("token")
        )
        GROUP_NAME = GROUP_NAME or cfg.get("GITLAB_GROUP") or cfg.get("group")
        GITHUB_TOKEN = (
            GITHUB_TOKEN or cfg.get("GITHUB_TOKEN") or cfg.get("github_token")
        )
        cfg_url = (
            cfg.get("GITLAB_URL")
            or cfg.get("GITLAB_HOSTNAME")
            or cfg.get("gitlab_url")
        )
        if cfg_url and GITLAB_URL == "https://gitlab.com" and not _args.gitlab_url:
            GITLAB_URL = cfg_url
        if cfg.get("project_list_file"):
            PROJECT_LIST_FILE = cfg["project_list_file"]
        mrv = cfg.get("migrate_repo_values")
        if isinstance(mrv, list):
            MIGRATE_REPO_VALUES = mrv
        elif isinstance(mrv, str):
            MIGRATE_REPO_VALUES = [mrv]
    except Exception as exc:
        log(f"ERROR: Failed to parse {_gl_migrate_config_file}: {exc}")
        sys.exit(1)
elif _token_file.exists():
    try:
        log(f"Reading config: {_token_file}")
        with open(_token_file, "r", encoding="utf-8") as f:
            cfg = json.load(f)
        GITLAB_TOKEN = (
            GITLAB_TOKEN
            or cfg.get("GITLAB_TOKEN")
            or cfg.get("GITLAB_API_PRIVATE_TOKEN")
            or cfg.get("token")
        )
        GROUP_NAME = GROUP_NAME or cfg.get("GITLAB_GROUP") or cfg.get("group")
        GITHUB_TOKEN = (
            GITHUB_TOKEN or cfg.get("GITHUB_TOKEN") or cfg.get("github_token")
        )
        cfg_url = cfg.get("GITLAB_URL") or cfg.get("gitlab_url")
        if cfg_url and GITLAB_URL == "https://gitlab.com" and not _args.gitlab_url:
            GITLAB_URL = cfg_url
        if cfg.get("project_list_file"):
            PROJECT_LIST_FILE = cfg["project_list_file"]
        mrv = cfg.get("migrate_repo_values")
        if isinstance(mrv, list):
            MIGRATE_REPO_VALUES = mrv
        elif isinstance(mrv, str):
            MIGRATE_REPO_VALUES = [mrv]
    except Exception as exc:
        log(f"ERROR: Failed to parse {_token_file}: {exc}")
        sys.exit(1)

if not MIGRATE_REPO_VALUES:
    MIGRATE_REPO_VALUES = ["Migrate"]

if not GROUP_NAME:
    log(
        "ERROR: No GitLab group defined. Set --group, env GITLAB_GROUP, "
        "or 'group' in gl-migrate.conf / .token."
    )
    sys.exit(1)

# Collect tokens (CLI > tokens-file > GITLAB_TOKENS > GITLAB_TOKEN).
tokens = load_tokens(
    args_tokens=_args.tokens or _args.token,
    tokens_file=_args.tokens_file,
)
if not tokens and GITLAB_TOKEN:
    tokens = [GITLAB_TOKEN]
if not tokens:
    log(
        "ERROR: No GitLab token(s). Set --token / --tokens / --tokens-file "
        "or GITLAB_TOKEN / GITLAB_TOKENS env var."
    )
    sys.exit(1)

OUTPUT_FILE = (
    Path(_args.output) if _args.output else data_dir / "gitlab-stats.csv"
)
CHECKPOINT_FILE = (
    Path(_args.checkpoint)
    if _args.checkpoint
    else data_dir / ".processed_projects"
)

log(f"GitLab instance: {GITLAB_URL}")
log(f"Group: {GROUP_NAME}")
log(f"Tokens: {len(tokens)} loaded")
log(f"Workers per token: {_args.workers_per_token}")
log(f"Total concurrent workers: {len(tokens) * _args.workers_per_token}")
log(f"Output: {OUTPUT_FILE}")
log(
    f"Checkpoint: {CHECKPOINT_FILE} "
    f"({'disabled' if _args.no_resume else 'enabled'})"
)
log(
    "Per-branch walks: "
    f"{'OFF (--no-branch-walk)' if _args.no_branch_walk else 'ON'}"
)
if GITHUB_TOKEN:
    log("GitHub token also available (not used by this script).")

logging.basicConfig(
    level=logging.DEBUG if _args.verbose else logging.WARNING,
    format="%(asctime)s [%(levelname)s] %(message)s",
)

pool = TokenPool(tokens, min_remaining=_args.rate_limit_floor)
client = PooledClient(GITLAB_URL, pool)


# ---------------------------------------------------------------------------
# Project filter (optional CSV pre-filter)
# ---------------------------------------------------------------------------

def load_project_filter() -> set[str] | None:
    if not PROJECT_LIST_FILE:
        log(
            "No project list file configured. "
            "Will process all projects in group."
        )
        return None
    project_list_path = data_dir / PROJECT_LIST_FILE
    if not project_list_path.exists():
        log(f"WARNING: Project list file not found: {project_list_path}")
        log("Falling back to processing all projects.")
        return None
    try:
        log(f"Loading project list from: {project_list_path}")
        wanted: set[str] = set()
        with open(project_list_path, "r", encoding="utf-8") as f:
            reader = csv.DictReader(f)
            if (
                not reader.fieldnames
                or "Migrate Repo" not in reader.fieldnames
                or "Name" not in reader.fieldnames
            ):
                log(
                    "ERROR: Required columns 'Name' or 'Migrate Repo' "
                    "not found in CSV. Falling back to all projects."
                )
                return None
            for row in reader:
                migrate_value = (row.get("Migrate Repo") or "").strip()
                project_name = (row.get("Name") or "").strip()
                if project_name and migrate_value in MIGRATE_REPO_VALUES:
                    wanted.add(project_name)
        log(f"Loaded {len(wanted)} project(s) from filter")
        if not wanted:
            log(
                f"WARNING: No projects matched filter values: "
                f"{MIGRATE_REPO_VALUES}. Falling back to all projects."
            )
            return None
        return wanted
    except Exception as exc:
        log(f"ERROR: Failed to load project filter file: {exc}")
        log("Falling back to processing all projects.")
        return None


project_filter = load_project_filter()


def should_process_project(project: dict, flt: set[str] | None) -> bool:
    if flt is None:
        return True
    return (
        project.get("name", "") in flt
        or project.get("path", "") in flt
    )


# ---------------------------------------------------------------------------
# Utility
# ---------------------------------------------------------------------------

def bytes_to_mb(b: int | float | None) -> float:
    if b is None:
        return 0
    return round(b / (1024 * 1024), 2)


def build_subgroup_columns(path_with_namespace: str) -> dict:
    """parent_group, subgroups, subgroup_count from `org/sub1/sub2/repo`."""
    parts = (path_with_namespace or "").split("/")
    groups = parts[:-1]
    out = {"parent_group": "", "subgroups": "", "subgroup_count": 0}
    if groups:
        out["parent_group"] = groups[0]
    if len(groups) > 1:
        out["subgroups"] = ",".join(groups[1:])
        out["subgroup_count"] = len(groups) - 1
    return out


# ---------------------------------------------------------------------------
# CSV schema (must stay aligned with project_stats dict below)
# ---------------------------------------------------------------------------

CSV_FIELDS = [
    "id",
    "name",
    "parent_group",
    "subgroups",
    "subgroup_count",
    "path",
    "status",
    "archived",
    "stars",
    "forks",
    "open_issues",
    "last_activity",
    "contributors",
    "pr_count",
    "total_commits",
    "branch_count",
    "file_count",
    "all_branches_file_count",
    "total_objects",
    "repository_size_mb",
    "repository_size_gb",
    "total_size_mb",
    "total_size_gb",
    "has_large_file_100mb",
    "exceeds_2gb",
    "exceeds_6gb",
    "pipeline",
    "has_lfs",
    "lfs_file_count",
    "lfs_total_size_bytes",
    "lfs_total_size_mb",
    "has_gitmodules",
    "has_codeowners",
    "has_pr_template",
    "releases_count",
    "branch_protections",
    "has_rulesets",
    "ruleset_count",
    "visibility",
    "created_at",
    "default_branch",
    "web_url",
    "exportable_users",
    "exportable_protected_branches",
    "exportable_merge_requests",
    "exportable_mr_notes",
    "exportable_issues",
    "exportable_issue_notes",
    "exportable_webhooks",
    "exportable_tags",
    "exportable_commit_comments",
    "exportable_has_wiki",
    "exportable_milestones",
]


# ---------------------------------------------------------------------------
# Data-collection helpers (all use the module-level `client`)
# ---------------------------------------------------------------------------

def _proj_base(project_id: int) -> str:
    return f"/projects/{project_id}"


def _head_count(path: str, extra_params: dict | None = None) -> int:
    params = {"per_page": 1}
    if extra_params:
        params.update(extra_params)
    try:
        resp = client.head(path, params=params)
        if resp.status_code == 200 and "X-Total" in resp.headers:
            try:
                return int(resp.headers["X-Total"])
            except ValueError:
                return 0
    except Exception:
        return 0
    return 0


def get_branch_count(project_id: int) -> int:
    path = f"{_proj_base(project_id)}/repository/branches"
    n = _head_count(path)
    if n:
        return n
    # Fallback: full pagination (accurate)
    try:
        return sum(1 for _ in client.paginated(path))
    except Exception as exc:
        logging.warning("branch count failed for %s: %s", project_id, exc)
        return 0


def get_exportable_model_counts(project_id: int) -> dict:
    base = _proj_base(project_id)
    counts = {
        "users_count": 0,
        "protected_branches": 0,
        "merge_requests": 0,
        "merge_request_notes": 0,
        "issues": 0,
        "issue_notes": 0,
        "webhooks": 0,
        "tags": 0,
        "commit_comments": 0,
        "has_wiki": False,
        "milestones": 0,
    }
    try:
        counts["users_count"] = _head_count(f"{base}/members/all")
        counts["protected_branches"] = _head_count(f"{base}/protected_branches")
        counts["merge_requests"] = _head_count(
            f"{base}/merge_requests", {"state": "all"}
        )

        # MR notes: sample first 5 MRs to estimate
        if counts["merge_requests"] > 0:
            resp = client.get(
                f"{base}/merge_requests",
                params={"state": "all", "per_page": 10},
            )
            if resp.status_code == 200:
                mrs = resp.json()
                total_notes = 0
                sample = mrs[:5]
                for mr in sample:
                    total_notes += _head_count(
                        f"{base}/merge_requests/{mr['iid']}/notes"
                    )
                if sample:
                    avg = total_notes / len(sample)
                    counts["merge_request_notes"] = int(
                        avg * counts["merge_requests"]
                    )

        counts["issues"] = _head_count(f"{base}/issues")
        if counts["issues"] > 0:
            resp = client.get(f"{base}/issues", params={"per_page": 10})
            if resp.status_code == 200:
                issues = resp.json()
                total_notes = 0
                sample = issues[:5]
                for issue in sample:
                    total_notes += _head_count(
                        f"{base}/issues/{issue['iid']}/notes"
                    )
                if sample:
                    avg = total_notes / len(sample)
                    counts["issue_notes"] = int(avg * counts["issues"])

        counts["webhooks"] = _head_count(f"{base}/hooks")
        counts["tags"] = _head_count(f"{base}/repository/tags")

        # Commit comments — sum of samples from first 5 commits.
        resp = client.get(
            f"{base}/repository/commits", params={"per_page": 10}
        )
        if resp.status_code == 200:
            commits = resp.json()
            total_comments = 0
            for commit in commits[:5]:
                total_comments += _head_count(
                    f"{base}/repository/commits/{commit['id']}/comments"
                )
            counts["commit_comments"] = total_comments

        # Wiki
        proj_resp = client.get(_proj_base(project_id))
        if proj_resp.status_code == 200:
            proj_info = proj_resp.json()
            if proj_info.get("wiki_enabled", False):
                wiki_count = _head_count(f"{base}/wikis")
                if wiki_count > 0:
                    counts["has_wiki"] = True
                else:
                    # Fallback: full GET
                    resp = client.get(f"{base}/wikis", params={"per_page": 1})
                    if resp.status_code == 200:
                        try:
                            counts["has_wiki"] = len(resp.json()) > 0
                        except Exception:
                            pass

        counts["milestones"] = _head_count(f"{base}/milestones")

    except Exception as exc:
        logging.warning(
            "exportable counts failed for %s: %s", project_id, exc
        )
    return counts


def get_repository_file_count(project_id: int) -> int:
    """File count on the default branch (capped at 50 pages = 5,000 files)."""
    try:
        resp = client.get(_proj_base(project_id))
        if resp.status_code != 200:
            return 0
        proj_info = resp.json()
        default_branch = proj_info.get("default_branch")
        if not default_branch:
            return 0

        repo_size_mb = (
            proj_info.get("statistics", {}).get("repository_size", 0)
            / (1024 * 1024)
        )

        # For very large repos try the search API first (cheap if it works).
        if repo_size_mb > 10000:
            try:
                resp = client.head(
                    f"{_proj_base(project_id)}/search",
                    params={"scope": "blobs", "search": "*"},
                )
                if (
                    resp.status_code == 200
                    and "X-Total" in resp.headers
                ):
                    try:
                        n = int(resp.headers["X-Total"])
                        if n > 0:
                            return n
                    except ValueError:
                        pass
            except Exception:
                pass

        # Standard pagination
        all_files: list = []
        page = 1
        max_pages = 50
        per_page = 100
        while page <= max_pages:
            resp = client.get(
                f"{_proj_base(project_id)}/repository/tree",
                params={
                    "recursive": "true",
                    "per_page": per_page,
                    "page": page,
                    "ref": default_branch,
                },
            )
            if resp.status_code == 404:
                return 0
            if resp.status_code != 200:
                break
            items = resp.json()
            if not items:
                break
            all_files.extend(i for i in items if i.get("type") == "blob")
            nxt = resp.headers.get("X-Next-Page")
            if not nxt:
                break
            try:
                page = int(nxt)
            except ValueError:
                break
        return len(all_files)
    except Exception as exc:
        logging.warning(
            "file count failed for %s: %s", project_id, exc
        )
        return 0


def check_for_pipeline_config(project_id: int) -> bool:
    """Look for a GitLab CI config file on the default branch."""
    try:
        resp = client.get(_proj_base(project_id))
        if resp.status_code != 200:
            return False
        default_branch = resp.json().get("default_branch")
        if not default_branch:
            return False
        candidates = [
            ".gitlab-ci.yml",
            ".gitlab-ci.yaml",
            "gitlab-ci.yml",
            "gitlab-ci.yaml",
            ".gitlab/ci.yml",
            ".gitlab/ci.yaml",
        ]
        for fp in candidates:
            encoded = quote(fp, safe="")
            r = client.head(
                f"{_proj_base(project_id)}/repository/files/{encoded}",
                params={"ref": default_branch},
            )
            if r.status_code == 200:
                return True
        return False
    except Exception as exc:
        logging.warning(
            "pipeline-config check failed for %s: %s", project_id, exc
        )
        return False


def check_file_exists(
    project_id: int, file_path: str, ref_branch: str
) -> bool:
    if not ref_branch:
        return False
    try:
        encoded = quote(file_path, safe="")
        r = client.head(
            f"{_proj_base(project_id)}/repository/files/{encoded}",
            params={"ref": ref_branch},
        )
        return r.status_code == 200
    except Exception:
        return False


def get_releases_count(project_id: int) -> int:
    path = f"{_proj_base(project_id)}/releases"
    n = _head_count(path)
    if n:
        return n
    try:
        return sum(1 for _ in client.paginated(path))
    except Exception:
        return 0


def check_lfs_enabled(project_id: int) -> dict:
    """Detect Git LFS via statistics, .gitattributes, and LFS API."""
    info = {
        "has_lfs": False,
        "lfs_file_count": 0,
        "lfs_total_size_bytes": 0,
        "lfs_total_size_mb": 0,
    }
    default_branch: str | None = None
    lfs_patterns: list[str] = []

    # Step 0: project statistics is the most reliable size source
    try:
        resp = client.get(
            _proj_base(project_id), params={"statistics": "true"}
        )
        if resp.status_code == 200:
            proj_info = resp.json()
            default_branch = proj_info.get("default_branch")
            stats = proj_info.get("statistics", {}) or {}
            lfs_size = stats.get("lfs_objects_size", 0) or 0
            if lfs_size > 0:
                info["has_lfs"] = True
                info["lfs_total_size_bytes"] = int(lfs_size)
                info["lfs_total_size_mb"] = round(lfs_size / (1024 * 1024), 2)
    except Exception:
        pass

    # Step 1: .gitattributes patterns
    try:
        if not default_branch:
            resp = client.get(_proj_base(project_id))
            if resp.status_code == 200:
                default_branch = resp.json().get("default_branch")
        if default_branch:
            encoded = quote(".gitattributes", safe="")
            r = client.get(
                f"{_proj_base(project_id)}/repository/files/{encoded}",
                params={"ref": default_branch},
            )
            if r.status_code == 200:
                content_b64 = r.json().get("content", "") or ""
                try:
                    decoded = base64.b64decode(content_b64).decode("utf-8")
                except Exception:
                    decoded = ""
                for line in decoded.splitlines():
                    line = line.strip()
                    if (
                        line
                        and "filter=lfs" in line
                        and not line.startswith("#")
                    ):
                        info["has_lfs"] = True
                        parts = line.split()
                        if parts:
                            lfs_patterns.append(parts[0])
    except Exception:
        pass

    # Step 2: LFS objects API (may 404 on some installs)
    try:
        r = client.get(f"{_proj_base(project_id)}/lfs_objects")
        if r.status_code == 200:
            objs = r.json()
            if isinstance(objs, list) and objs:
                info["has_lfs"] = True
                info["lfs_file_count"] = len(objs)
                if info["lfs_total_size_bytes"] == 0:
                    total = sum(int(o.get("size", 0) or 0) for o in objs)
                    info["lfs_total_size_bytes"] = total
                    info["lfs_total_size_mb"] = round(total / (1024 * 1024), 2)
    except Exception:
        pass

    # Step 3: tree scan fallback (limited to 500 items, basename match)
    if info["has_lfs"] and info["lfs_file_count"] == 0 and lfs_patterns and default_branch:
        try:
            r = client.get(
                f"{_proj_base(project_id)}/repository/tree",
                params={
                    "recursive": "true",
                    "per_page": 100,
                    "ref": default_branch,
                },
            )
            if r.status_code == 200:
                items = r.json()
                lfs_matches = 0
                for item in items[:500]:
                    if item.get("type") != "blob":
                        continue
                    file_path = item.get("path", "") or ""
                    if not file_path:
                        continue
                    basename = os.path.basename(file_path)
                    for pat in lfs_patterns:
                        # Match against both basename and full path so patterns
                        # like "*.psd" and "assets/*.png" both work.
                        if (
                            fnmatch.fnmatch(basename, pat)
                            or fnmatch.fnmatch(file_path, pat)
                        ):
                            lfs_matches += 1
                            break
                if lfs_matches > 0:
                    info["lfs_file_count"] = lfs_matches
        except Exception:
            pass

    return info


def get_all_branches_commit_count(project_id: int) -> int:
    """Count unique commit SHAs across every branch (slow)."""
    try:
        shas: set[str] = set()
        branches_path = f"{_proj_base(project_id)}/repository/branches"
        for branch in client.paginated(branches_path):
            name = branch.get("name")
            if not name:
                continue
            try:
                for commit in client.paginated(
                    f"{_proj_base(project_id)}/repository/commits",
                    params={"ref_name": name},
                ):
                    sha = commit.get("id")
                    if sha:
                        shas.add(sha)
            except Exception as exc:
                logging.warning(
                    "commits for branch %s failed: %s", name, exc
                )
                continue
        return len(shas)
    except Exception as exc:
        logging.warning(
            "all-branches commit count failed for %s: %s", project_id, exc
        )
        return 0


def get_all_branches_file_stats(project_id: int) -> dict:
    """File count + large-file flag + total bytes across every branch (slow)."""
    LARGE_EXTS = (
        ".zip", ".tar", ".gz", ".iso", ".dmg", ".exe", ".deb", ".rpm",
        ".pkg", ".msi", ".war", ".ear", ".jar", ".pdf", ".mp4",
        ".mov", ".avi", ".mkv", ".mp3", ".wav", ".flac",
    )
    stats = {"total_files": 0, "has_large_file": False, "total_bytes": 0}
    unique_blobs: set[tuple[str, str | None]] = set()
    large_found = False
    try:
        for branch in client.paginated(
            f"{_proj_base(project_id)}/repository/branches"
        ):
            name = branch.get("name")
            if not name:
                continue
            try:
                for item in client.paginated(
                    f"{_proj_base(project_id)}/repository/tree",
                    params={"recursive": "true", "ref": name},
                ):
                    if item.get("type") != "blob":
                        continue
                    fp = item.get("path", "") or ""
                    if not fp:
                        continue
                    key = (fp, item.get("id"))
                    if key not in unique_blobs:
                        unique_blobs.add(key)
                        stats["total_bytes"] += item.get("size", 0) or 0
                    if not large_found and fp.lower().endswith(LARGE_EXTS):
                        try:
                            encoded = quote(fp, safe="")
                            r = client.head(
                                f"{_proj_base(project_id)}/repository/files/{encoded}",
                                params={"ref": name},
                            )
                            if r.status_code == 200:
                                try:
                                    sz = int(
                                        r.headers.get("X-Gitlab-Size", "0")
                                    )
                                    if sz > 100 * 1024 * 1024:
                                        large_found = True
                                except ValueError:
                                    pass
                        except Exception:
                            pass
            except Exception as exc:
                logging.warning(
                    "tree for branch %s failed: %s", name, exc
                )
                continue
    except Exception as exc:
        logging.warning(
            "all-branches file stats failed for %s: %s", project_id, exc
        )
    stats["total_files"] = len(unique_blobs)
    stats["has_large_file"] = large_found
    return stats


def get_repository_object_count(
    project_id: int, existing_stats: dict
) -> int:
    try:
        n = existing_stats.get("commit_count", 0)
        n += existing_stats.get("branch_count", 0)
        n += _head_count(f"{_proj_base(project_id)}/repository/tags")
        file_count = existing_stats.get(
            "all_branches_file_count",
            existing_stats.get("file_count", 0),
        )
        n += file_count + max(file_count // 10, 1)
        n += _head_count(
            f"{_proj_base(project_id)}/merge_requests", {"state": "all"}
        )
        return n
    except Exception:
        return existing_stats.get("commit_count", 0) + existing_stats.get(
            "file_count", 0
        )


def get_repository_stats(project_id: int, no_branch_walk: bool) -> dict:
    stats = {
        "file_count": 0,
        "repository_size": 0,
        "storage_size": 0,
        "commit_count": 0,
        "branch_count": 0,
        "object_count": 0,
        "all_branches_file_count": 0,
        "has_large_file": False,
        "exceeds_6gb": False,
        "exceeds_2gb": False,
        "has_pipeline": False,
    }
    try:
        resp = client.get(
            _proj_base(project_id),
            params={"statistics": "true", "license": "true"},
        )
        if resp.status_code == 200:
            proj = resp.json()
            api_stats = proj.get("statistics", {}) or {}
            stats["repository_size"] = api_stats.get("repository_size", 0)
            stats["storage_size"] = api_stats.get("storage_size", 0)
            stats["commit_count"] = api_stats.get("commit_count", 0)
            ss = stats["storage_size"]
            stats["exceeds_2gb"] = ss > (2 * 1024 ** 3)
            stats["exceeds_6gb"] = ss > (6 * 1024 ** 3)

        stats["file_count"] = get_repository_file_count(project_id)
        stats["branch_count"] = get_branch_count(project_id)

        if not no_branch_walk:
            cnt = get_all_branches_commit_count(project_id)
            if cnt > 0:
                stats["commit_count"] = cnt

        stats["has_pipeline"] = check_for_pipeline_config(project_id)

        if no_branch_walk:
            # Keep file_count as the single-branch number; no large-file
            # detection. has_large_file stays False; all_branches_file_count
            # equals file_count for stability.
            stats["all_branches_file_count"] = stats["file_count"]
        else:
            br = get_all_branches_file_stats(project_id)
            stats["all_branches_file_count"] = br["total_files"]
            stats["has_large_file"] = br["has_large_file"]
            if stats["repository_size"] == 0 and br["total_bytes"] > 0:
                stats["repository_size"] = br["total_bytes"]
                stats["storage_size"] = br["total_bytes"]

        stats["object_count"] = get_repository_object_count(project_id, stats)
    except Exception as exc:
        logging.warning(
            "repo stats failed for %s: %s", project_id, exc
        )
    return stats


# ---------------------------------------------------------------------------
# Per-project worker (thread)
# ---------------------------------------------------------------------------

PR_TEMPLATE_PATHS = [
    ".gitlab/merge_request_templates/default.md",
    ".gitlab/merge_request_templates/Default.md",
    ".gitlab/merge_request_templates/merge_request_template.md",
    "PULL_REQUEST_TEMPLATE.md",
    ".github/PULL_REQUEST_TEMPLATE.md",
    "docs/pull_request_template.md",
]

CODEOWNERS_PATHS = [
    "CODEOWNERS",
    ".gitlab/CODEOWNERS",
    "docs/CODEOWNERS",
]


def collect_project_stats(project: dict, no_branch_walk: bool) -> dict:
    project_id = project["id"]
    default_branch = project.get("default_branch") or ""
    is_archived = bool(project.get("archived", False))

    # contributors (default branch only — API limitation)
    contributors: list = []
    try:
        resp = client.get(f"{_proj_base(project_id)}/repository/contributors")
        if resp.status_code == 200:
            contributors = resp.json() or []
    except Exception as exc:
        logging.warning(
            "contributors failed for %s: %s", project_id, exc
        )

    repo_stats = get_repository_stats(project_id, no_branch_walk)

    repository_size = repo_stats["repository_size"]
    storage_size = repo_stats["storage_size"]

    # Fallback to the project list's own statistics block
    if repository_size == 0 and storage_size == 0:
        s = project.get("statistics") or {}
        repository_size = s.get("repository_size", 0) or 0
        storage_size = s.get("storage_size", 0) or 0

    exceeds_2gb = storage_size > (2 * 1024 ** 3)
    exceeds_6gb = storage_size > (6 * 1024 ** 3)

    model_counts = get_exportable_model_counts(project_id)
    lfs_info = check_lfs_enabled(project_id)

    has_gitmodules = check_file_exists(
        project_id, ".gitmodules", default_branch
    )
    has_codeowners = any(
        check_file_exists(project_id, p, default_branch)
        for p in CODEOWNERS_PATHS
    )
    has_pr_template = any(
        check_file_exists(project_id, p, default_branch)
        for p in PR_TEMPLATE_PATHS
    )
    releases_count = get_releases_count(project_id)

    path_with_ns = project.get("path_with_namespace", "")
    subgroup_cols = build_subgroup_columns(path_with_ns)

    return {
        "id": project_id,
        "name": project.get("name", ""),
        **subgroup_cols,
        "path": path_with_ns,
        "status": "archived" if is_archived else "active",
        "archived": is_archived,
        "stars": project.get("star_count", 0),
        "forks": project.get("forks_count", 0),
        "open_issues": project.get("open_issues_count", 0),
        "last_activity": project.get("last_activity_at", "N/A"),
        "contributors": len(contributors),
        "pr_count": model_counts["merge_requests"],
        "total_commits": repo_stats.get("commit_count", 0),
        "branch_count": repo_stats.get("branch_count", 0),
        "file_count": repo_stats.get("file_count", 0),
        "all_branches_file_count": repo_stats.get(
            "all_branches_file_count", repo_stats.get("file_count", 0)
        ),
        "total_objects": repo_stats.get("object_count", 0),
        "repository_size_mb": bytes_to_mb(repository_size),
        "repository_size_gb": round(bytes_to_mb(repository_size) / 1024, 2),
        "total_size_mb": bytes_to_mb(storage_size),
        "total_size_gb": round(bytes_to_mb(storage_size) / 1024, 2),
        "has_large_file_100mb": repo_stats.get("has_large_file", False),
        "exceeds_2gb": exceeds_2gb,
        "exceeds_6gb": exceeds_6gb,
        "pipeline": repo_stats.get("has_pipeline", False),
        "has_lfs": lfs_info["has_lfs"],
        "lfs_file_count": lfs_info["lfs_file_count"],
        "lfs_total_size_bytes": lfs_info["lfs_total_size_bytes"],
        "lfs_total_size_mb": lfs_info["lfs_total_size_mb"],
        "has_gitmodules": has_gitmodules,
        "has_codeowners": has_codeowners,
        "has_pr_template": has_pr_template,
        "releases_count": releases_count,
        "branch_protections": model_counts["protected_branches"],
        "has_rulesets": False,  # GitHub-only
        "ruleset_count": 0,     # GitHub-only
        "visibility": project.get("visibility", "N/A"),
        "created_at": project.get("created_at", "N/A"),
        "default_branch": default_branch,
        "web_url": project.get("web_url", "N/A"),
        "exportable_users": model_counts["users_count"],
        "exportable_protected_branches": model_counts["protected_branches"],
        "exportable_merge_requests": model_counts["merge_requests"],
        "exportable_mr_notes": model_counts["merge_request_notes"],
        "exportable_issues": model_counts["issues"],
        "exportable_issue_notes": model_counts["issue_notes"],
        "exportable_webhooks": model_counts["webhooks"],
        "exportable_tags": model_counts["tags"],
        "exportable_commit_comments": model_counts["commit_comments"],
        "exportable_has_wiki": model_counts["has_wiki"],
        "exportable_milestones": model_counts["milestones"],
    }


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def _open_sink_with_fallback(append: bool) -> tuple[CsvSink, Path]:
    """
    Try to open OUTPUT_FILE. If it's locked (e.g. open in Excel), fall back
    to a timestamped backup name in the same directory.
    """
    try:
        return CsvSink(OUTPUT_FILE, CSV_FIELDS, append=append), OUTPUT_FILE
    except (PermissionError, OSError) as exc:
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        backup = OUTPUT_FILE.parent / (
            f"{OUTPUT_FILE.stem}_backup_{ts}{OUTPUT_FILE.suffix}"
        )
        log(
            f"WARNING: Could not open {OUTPUT_FILE}: {exc}. "
            f"Writing to {backup} instead."
        )
        return CsvSink(backup, CSV_FIELDS, append=False), backup


def main() -> int:
    log(f"Fetching all projects from group: {GROUP_NAME}")
    try:
        group_input: str | int = GROUP_NAME
        if isinstance(group_input, str) and group_input.isdigit():
            group_input = int(group_input)
        # Resolve the group first (also URL-encodes nested paths properly).
        root = get_group(client, group_input)
        log(f"Root group: {root['full_path']} (id={root['id']})")
        projects = list(
            list_group_projects(
                client,
                root["id"],
                statistics=True,
                # archived included so we report it in the CSV (existing
                # behaviour).
            )
        )
    except Exception as exc:
        log(f"ERROR: Failed to fetch projects: {exc}")
        return 1

    log(f"Successfully fetched {len(projects)} project(s) from GitLab")

    if project_filter is not None:
        before = len(projects)
        projects = [
            p for p in projects
            if should_process_project(p, project_filter)
        ]
        log(
            f"After CSV filter: {len(projects)} of {before} project(s) "
            "will be processed"
        )

    if not projects:
        log("WARNING: No projects to process after filtering")
        return 0

    # Checkpoint / resume
    checkpoint: CheckpointStore | None = None
    if not _args.no_resume:
        checkpoint = CheckpointStore(CHECKPOINT_FILE)
        if checkpoint.done_count:
            before = len(projects)
            projects = [
                p for p in projects
                if not checkpoint.is_done(int(p["id"]))
            ]
            log(
                f"Resume: skipping {before - len(projects)} already-"
                f"processed project(s); {len(projects)} remaining"
            )

    if not projects:
        log("Nothing left to do — checkpoint already complete.")
        return 0

    sink, sink_path = _open_sink_with_fallback(
        append=bool(checkpoint and checkpoint.done_count)
    )

    total = len(projects)
    done = 0
    failed_projects: list[str] = []
    start = datetime.now()
    max_workers = max(1, len(pool) * _args.workers_per_token)

    log(f"Processing {total} project(s) with {max_workers} worker(s)...")

    try:
        with ThreadPoolExecutor(max_workers=max_workers) as ex:
            futures = {
                ex.submit(
                    collect_project_stats, p, _args.no_branch_walk
                ): p
                for p in projects
            }
            for fut in as_completed(futures):
                project = futures[fut]
                pid = project["id"]
                pname = project.get("name", str(pid))
                try:
                    row = fut.result()
                except Exception as exc:
                    failed_projects.append(pname)
                    log(f"  [fail] {pname} (id={pid}): {exc}")
                    continue
                sink.write(row)
                done += 1
                if checkpoint:
                    checkpoint.mark_done(int(pid))
                log(
                    f"  [ok {done}/{total}] {row['path']}  "
                    f"size={row['repository_size_mb']}MB  "
                    f"branches={row['branch_count']}  "
                    f"files={row['file_count']}  "
                    f"commits={row['total_commits']}  "
                    f"lfs={row['has_lfs']}"
                )
                if done % 25 == 0 or done == total:
                    elapsed = (datetime.now() - start).total_seconds()
                    rate = done / max(elapsed, 1.0)
                    eta = (total - done) / max(rate, 0.001)
                    log(
                        f"[progress] {done}/{total} done  "
                        f"({rate:.2f} proj/s, elapsed {elapsed:.0f}s, "
                        f"eta ~{eta:.0f}s)"
                    )
    finally:
        sink.close()

    log("")
    log(f"Completed {done}/{total} project(s).")
    if failed_projects:
        log(
            f"Failed: {len(failed_projects)} project(s) "
            f"-> {failed_projects[:10]}"
            + (f" (+{len(failed_projects) - 10} more)"
               if len(failed_projects) > 10 else "")
        )
    if project_filter is not None:
        log(f"Filter file: {PROJECT_LIST_FILE}")
        log(f"Filter values: {MIGRATE_REPO_VALUES}")
    log(f"CSV written to: {sink_path}")

    return 0


if __name__ == "__main__":
    try:
        exit_code = main()
    finally:
        # Always print the timing summary, even on failure.
        elapsed = datetime.now() - script_start_time
        total_seconds = int(elapsed.total_seconds())
        hours = total_seconds // 3600
        minutes = (total_seconds % 3600) // 60
        seconds = total_seconds % 60
        log("")
        log("=" * 50)
        if hours:
            log(
                f"Script execution time: {hours}h {minutes}m {seconds}s"
            )
        elif minutes:
            log(f"Script execution time: {minutes}m {seconds}s")
        else:
            log(f"Script execution time: {seconds}s")
        log("=" * 50)
        log("Script completed")
    raise SystemExit(exit_code)
