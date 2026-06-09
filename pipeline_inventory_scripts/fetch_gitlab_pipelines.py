"""
Parallel GitLab pipeline & CI/CD inventory.

For every project under a GitLab group (recursing all nested subgroups),
collect five kinds of rows into one CSV:

  * pipeline_run      — every actual pipeline run (all branches/tags/statuses)
  * ci_config         — `.gitlab-ci.yml` or custom `ci_config_path`
                        (including external `path@group/project:ref`)
  * reusable_template — YAML files in `templates/`, `ci-templates/`, `ci/`
                        directories (where group-level reusable pipelines
                        typically live)
  * deployment        — every deployment record (CD activity)
                        — enabled with `--with-deployments` or `--full`
  * environment       — every deployment target / environment
                        — enabled with `--with-environments` or `--full`

By default `ci_config` and `reusable_template` are scanned on the project's
default branch only. Pass `--all-branches` to scan EVERY branch of every
project — this is the only way to find branch-specific CI overrides.

Features
--------
* Multiple PATs with round-robin pool + per-token rate-limit tracking
* Configurable workers per token (ThreadPoolExecutor)
* Honors RateLimit-Remaining and Retry-After; retries on 429/5xx
* Resume support via a checkpoint file of completed project IDs
* `--projects-csv` to skip the group listing API call and reuse the
  project list from `repo_inventory_scripts/data/gitlab-stats.csv`

Usage
-----
    set GITLAB_TOKEN=glpat-xxx
    python fetch_gitlab_pipelines.py --group my-org

    REM Full inventory — everything on every branch, plus CD data
    python fetch_gitlab_pipelines.py --group my-org --full

    REM CI configs and templates on EVERY branch (a lot more API calls)
    python fetch_gitlab_pipelines.py --group my-org --all-branches

    REM CD visibility — deployments + environments
    python fetch_gitlab_pipelines.py --group my-org `
        --with-deployments --with-environments

    REM Just inventory, no run history (much faster)
    python fetch_gitlab_pipelines.py --group my-org --no-runs

    REM 3 PATs, 4 workers each = 12 concurrent
    set GITLAB_TOKENS=glpat-a,glpat-b,glpat-c
    python fetch_gitlab_pipelines.py --group my-org --workers-per-token 4

    REM Reuse the project list from the repo inventory CSV
    python fetch_gitlab_pipelines.py ^
        --projects-csv ../repo_inventory_scripts/data/gitlab-stats.csv --full

Note about throughput
---------------------
GitLab rate-limits are per authenticated user. Three PATs on the same user
share one budget. To actually get Nx throughput, each PAT must belong to a
distinct user (or service account).
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from urllib.parse import quote

# Import the shared client (file lives at the workspace root).
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from gitlab_client import (  # noqa: E402
    CheckpointStore,
    CsvSink,
    PooledClient,
    TokenPool,
    get_group,
    list_group_projects,
    load_projects_csv,
    load_tokens,
)


DEFAULT_GITLAB_URL = "https://gitlab.com"
DEFAULT_CI_FILE = ".gitlab-ci.yml"
TEMPLATE_DIRS = ("templates", "ci-templates", "ci")
YAML_EXTS = (".yml", ".yaml")

CSV_FIELDS = [
    "type",            # pipeline_run | ci_config | reusable_template
                       # | deployment | environment
    "pipeline_id",
    "group",
    "subgroup",
    "project",
    "branch",
    "status",
    "source",
    "sha",
    "file_path",
    "file_ref",
    "created_at",
    "updated_at",
    "web_url",
    "project_web_url",
    "project_id",
    "environment",     # NEW: env name for deployment/environment rows
]


# ---------------------------------------------------------------------------
# per-project helpers
# ---------------------------------------------------------------------------

def split_namespace(
    path_with_namespace: str, root_group: str
) -> tuple[str, str, str]:
    """Split `root/sub1/sub2/project` -> ('root', 'sub1/sub2', 'project')."""
    parts = (path_with_namespace or "").split("/")
    project = parts[-1] if parts else ""
    namespace_parts = parts[:-1]
    root_parts = (root_group or "").split("/") if root_group else []
    if root_parts and namespace_parts[: len(root_parts)] == root_parts:
        subgroup_parts = namespace_parts[len(root_parts):]
    else:
        root_parts = namespace_parts[:1]
        subgroup_parts = namespace_parts[1:]
    return ("/".join(root_parts), "/".join(subgroup_parts), project)


def parse_ci_config_path(raw: str | None) -> tuple[str, str | None]:
    """`ci_config_path` may be 'path/file.yml@group/project:ref'."""
    if not raw:
        return DEFAULT_CI_FILE, None
    path, _, external = raw.partition("@")
    return (path or DEFAULT_CI_FILE), (external or None)


def file_exists(
    client: PooledClient, project_id: int, file_path: str, ref: str
) -> bool:
    encoded = quote(file_path, safe="")
    resp = client.head(
        f"/projects/{project_id}/repository/files/{encoded}",
        params={"ref": ref},
    )
    return resp.status_code == 200


def list_tree(
    client: PooledClient, project_id: int, path: str, ref: str
) -> list[dict]:
    try:
        return list(client.paginated(
            f"/projects/{project_id}/repository/tree",
            params={"path": path, "ref": ref, "recursive": "true"},
        ))
    except RuntimeError:
        return []


def discover_reusable_yaml_on_branch(
    client: PooledClient, project_id: int, ref: str
) -> list[dict]:
    """Scan templates/, ci-templates/, ci/ on one specific branch."""
    out: list[dict] = []
    for top in TEMPLATE_DIRS:
        for node in list_tree(client, project_id, top, ref):
            path = node.get("path", "") or ""
            if node.get("type") == "blob" and path.lower().endswith(YAML_EXTS):
                out.append(node)
    return out


def list_branch_names(client: PooledClient, project_id: int) -> list[str]:
    """Return every branch name in a project (paginated)."""
    out: list[str] = []
    try:
        for b in client.paginated(
            f"/projects/{project_id}/repository/branches"
        ):
            name = b.get("name")
            if name:
                out.append(name)
    except Exception as exc:  # noqa: BLE001
        logging.warning(
            "could not list branches for project %s: %s", project_id, exc
        )
    return out


def list_pipeline_runs(client: PooledClient, project_id: int):
    """All pipeline runs (any branch/tag, any status)."""
    return client.paginated(
        f"/projects/{project_id}/pipelines",
        params={"order_by": "id", "sort": "desc"},
    )


def list_deployments(client: PooledClient, project_id: int):
    """All deployment records for CD visibility."""
    return client.paginated(
        f"/projects/{project_id}/deployments",
        params={"order_by": "id", "sort": "desc"},
    )


def list_environments(client: PooledClient, project_id: int):
    """All deploy-target environments."""
    return client.paginated(f"/projects/{project_id}/environments")


def file_web_url(project_web_url: str, ref: str, path: str) -> str:
    return f"{project_web_url}/-/blob/{ref}/{path}"


# ---------------------------------------------------------------------------
# per-project worker (runs in a thread)
# ---------------------------------------------------------------------------

def process_project(
    client: PooledClient,
    project: dict,
    root_path: str,
    sink: CsvSink,
    counts: dict,
    counts_lock: threading.Lock,
    include_runs: bool,
    include_templates: bool,
    all_branches: bool,
    include_deployments: bool,
    include_environments: bool,
) -> dict:
    full_path = project.get("path_with_namespace", "")
    group_name, subgroup, project_name = split_namespace(full_path, root_path)
    project_web = project.get("web_url", "") or ""
    default_branch = project.get("default_branch") or ""
    pid = int(project["id"])

    base_row = {
        "group": group_name,
        "subgroup": subgroup,
        "project": project_name,
        "project_web_url": project_web,
        "project_id": pid,
    }

    rows: list[dict] = []
    local = {
        "ci_config": 0,
        "reusable_template": 0,
        "pipeline_run": 0,
        "deployment": 0,
        "environment": 0,
    }

    # Branches to scan when --all-branches is on.
    # Fetched lazily and cached so we only paginate once per project.
    _branches_cache: list[str] | None = None

    def branches_to_scan() -> list[str]:
        nonlocal _branches_cache
        if not all_branches:
            return [default_branch] if default_branch else []
        if _branches_cache is None:
            _branches_cache = list_branch_names(client, pid)
        return _branches_cache

    # -- 1. Project CI config (.gitlab-ci.yml or custom path) ---------------
    ci_path, external_ref = parse_ci_config_path(project.get("ci_config_path"))
    ci_summary = "missing"

    if external_ref:
        # External include — per-project, not per-branch.
        rows.append({
            **base_row,
            "type": "ci_config",
            "pipeline_id": "",
            "branch": default_branch,
            "status": "external",
            "source": "",
            "sha": "",
            "file_path": ci_path,
            "file_ref": external_ref,
            "created_at": "",
            "updated_at": "",
            "web_url": "",
            "environment": "",
        })
        local["ci_config"] += 1
        ci_summary = "external"
    else:
        found_on: list[str] = []
        for branch in branches_to_scan():
            if not branch:
                continue
            try:
                if file_exists(client, pid, ci_path, branch):
                    found_on.append(branch)
                    rows.append({
                        **base_row,
                        "type": "ci_config",
                        "pipeline_id": "",
                        "branch": branch,
                        "status": "found",
                        "source": "",
                        "sha": "",
                        "file_path": ci_path,
                        "file_ref": branch,
                        "created_at": "",
                        "updated_at": "",
                        "web_url": file_web_url(project_web, branch, ci_path),
                        "environment": "",
                    })
                    local["ci_config"] += 1
            except Exception as exc:  # noqa: BLE001
                logging.warning(
                    "ci-config check failed for %s @ %s: %s",
                    full_path, branch, exc,
                )
        if found_on:
            ci_summary = (
                f"found:{len(found_on)}br" if all_branches else "found"
            )

    # -- 2. Reusable pipeline YAML (templates/, ci-templates/, ci/) ---------
    templates_total = 0
    if include_templates:
        for branch in branches_to_scan():
            if not branch:
                continue
            try:
                templates = discover_reusable_yaml_on_branch(
                    client, pid, branch
                )
            except Exception as exc:  # noqa: BLE001
                logging.warning(
                    "template scan failed for %s @ %s: %s",
                    full_path, branch, exc,
                )
                continue
            for node in templates:
                node_path = node.get("path", "")
                rows.append({
                    **base_row,
                    "type": "reusable_template",
                    "pipeline_id": "",
                    "branch": branch,
                    "status": "",
                    "source": "",
                    "sha": "",
                    "file_path": node_path,
                    "file_ref": branch,
                    "created_at": "",
                    "updated_at": "",
                    "web_url": file_web_url(project_web, branch, node_path),
                    "environment": "",
                })
                local["reusable_template"] += 1
                templates_total += 1

    # -- 3. Pipeline runs (every branch / tag / status) ---------------------
    run_count = 0
    if include_runs:
        try:
            for p in list_pipeline_runs(client, pid):
                rows.append({
                    **base_row,
                    "type": "pipeline_run",
                    "pipeline_id": p.get("id", ""),
                    "branch": p.get("ref", ""),
                    "status": p.get("status", ""),
                    "source": p.get("source", ""),
                    "sha": p.get("sha", ""),
                    "file_path": "",
                    "file_ref": "",
                    "created_at": p.get("created_at", ""),
                    "updated_at": p.get("updated_at", ""),
                    "web_url": p.get("web_url", ""),
                    "environment": "",
                })
                run_count += 1
            local["pipeline_run"] += run_count
        except RuntimeError as exc:
            logging.warning("pipelines fetch failed for %s: %s", full_path, exc)

    # -- 4. Deployments (CD activity) ---------------------------------------
    deployment_count = 0
    if include_deployments:
        try:
            for d in list_deployments(client, pid):
                env = d.get("environment") or {}
                deployable = d.get("deployable") or {}
                rows.append({
                    **base_row,
                    "type": "deployment",
                    "pipeline_id": d.get("iid", d.get("id", "")),
                    "branch": d.get("ref", ""),
                    "status": d.get("status", ""),
                    "source": deployable.get("name", ""),  # job name
                    "sha": d.get("sha", ""),
                    "file_path": "",
                    "file_ref": "",
                    "created_at": d.get("created_at", ""),
                    "updated_at": d.get("updated_at", ""),
                    "web_url": (
                        f"{project_web}/-/deployments/{d.get('iid', '')}"
                        if d.get("iid") else ""
                    ),
                    "environment": env.get("name", ""),
                })
                deployment_count += 1
            local["deployment"] += deployment_count
        except RuntimeError as exc:
            logging.warning(
                "deployments fetch failed for %s: %s", full_path, exc
            )

    # -- 5. Environments (deploy targets) -----------------------------------
    environment_count = 0
    if include_environments:
        try:
            for e in list_environments(client, pid):
                last = e.get("last_deployment") or {}
                rows.append({
                    **base_row,
                    "type": "environment",
                    "pipeline_id": e.get("id", ""),
                    "branch": last.get("ref", ""),
                    "status": e.get("state", ""),
                    "source": e.get("tier", "") or "",
                    "sha": last.get("sha", ""),
                    "file_path": "",
                    "file_ref": "",
                    "created_at": last.get("created_at", "") or "",
                    "updated_at": "",
                    "web_url": e.get("external_url", "") or "",
                    "environment": e.get("name", ""),
                })
                environment_count += 1
            local["environment"] += environment_count
        except RuntimeError as exc:
            logging.warning(
                "environments fetch failed for %s: %s", full_path, exc
            )

    sink.write_many(rows)
    with counts_lock:
        for k, v in local.items():
            counts[k] = counts.get(k, 0) + v

    return {
        "full_path": full_path,
        "ci_status": ci_summary,
        "templates": templates_total,
        "runs": run_count,
        "deployments": deployment_count,
        "environments": environment_count,
        "branches_scanned": (
            len(_branches_cache) if _branches_cache is not None
            else (1 if default_branch else 0)
        ),
    }


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )

    src = p.add_argument_group("project source (one is required)")
    src.add_argument(
        "--group",
        help=(
            "Root group full path (e.g. 'my-org') or numeric ID. "
            "All nested subgroups are walked."
        ),
    )
    src.add_argument(
        "--projects-csv",
        help=(
            "Reuse a gitlab-stats.csv from the repo inventory script "
            "(skips the group-walk API calls)."
        ),
    )

    auth = p.add_argument_group("authentication")
    auth.add_argument(
        "--gitlab-url",
        default=os.environ.get("GITLAB_URL", DEFAULT_GITLAB_URL),
        help=(
            f"GitLab base URL "
            f"(default: env GITLAB_URL or {DEFAULT_GITLAB_URL})."
        ),
    )
    auth.add_argument(
        "--token",
        help="Single PAT (default: env GITLAB_TOKEN).",
    )
    auth.add_argument(
        "--tokens",
        help=(
            "Comma-separated PATs for round-robin pool "
            "(default: env GITLAB_TOKENS)."
        ),
    )
    auth.add_argument(
        "--tokens-file",
        help="File with one PAT per line ('#' starts a comment).",
    )

    perf = p.add_argument_group("performance")
    perf.add_argument(
        "--workers-per-token",
        type=int,
        default=4,
        help="Concurrent worker threads per token (default 4).",
    )
    perf.add_argument(
        "--rate-limit-floor",
        type=int,
        default=50,
        help=(
            "Pause a token when RateLimit-Remaining drops below this "
            "(default 50)."
        ),
    )

    out = p.add_argument_group("output / resume")
    out.add_argument(
        "--output",
        default="./output/pipelines.csv",
        help="Output CSV path (default ./output/pipelines.csv).",
    )
    out.add_argument(
        "--checkpoint",
        default="./output/.processed_pipelines",
        help="Resume file of completed project IDs.",
    )
    out.add_argument(
        "--no-resume",
        action="store_true",
        help="Ignore the checkpoint and process every project.",
    )

    scope = p.add_argument_group("what to collect")
    scope.add_argument(
        "--no-runs",
        action="store_true",
        help="Skip pipeline_run rows (much faster).",
    )
    scope.add_argument(
        "--no-templates",
        action="store_true",
        help="Skip the reusable_template scan.",
    )
    scope.add_argument(
        "--all-branches",
        action="store_true",
        help=(
            "Scan ci_config and reusable_template on EVERY branch "
            "(default: default branch only). Multiplies API calls by the "
            "average branches-per-project."
        ),
    )
    scope.add_argument(
        "--with-deployments",
        action="store_true",
        help="Include `deployment` rows (CD activity per environment).",
    )
    scope.add_argument(
        "--with-environments",
        action="store_true",
        help="Include `environment` rows (deploy targets).",
    )
    scope.add_argument(
        "--full",
        action="store_true",
        help=(
            "Shortcut for --all-branches --with-deployments "
            "--with-environments. Pipeline runs stay on unless you also "
            "pass --no-runs."
        ),
    )

    p.add_argument("--verbose", "-v", action="store_true")
    args = p.parse_args(argv)

    # --full expands to the three feature flags.
    if args.full:
        args.all_branches = True
        args.with_deployments = True
        args.with_environments = True

    return args


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
    )

    if not args.group and not args.projects_csv:
        print(
            "error: --group or --projects-csv is required.",
            file=sys.stderr,
        )
        return 2

    tokens = load_tokens(
        args_tokens=args.tokens or args.token,
        tokens_file=args.tokens_file,
    )
    if not tokens:
        print(
            "error: no token provided. Use --token / --tokens / "
            "--tokens-file or env GITLAB_TOKEN / GITLAB_TOKENS.",
            file=sys.stderr,
        )
        return 2

    pool = TokenPool(tokens, min_remaining=args.rate_limit_floor)
    client = PooledClient(args.gitlab_url, pool)
    max_workers = max(1, len(pool) * args.workers_per_token)

    print(f"[info] GitLab URL: {args.gitlab_url}")
    print(f"[info] Tokens: {len(pool)}  ({', '.join(pool.masks)})")
    print(
        f"[info] Workers: {args.workers_per_token}/token = "
        f"{max_workers} concurrent"
    )
    print(
        "[info] Scope: "
        f"runs={not args.no_runs}, "
        f"templates={not args.no_templates}, "
        f"all_branches={args.all_branches}, "
        f"deployments={args.with_deployments}, "
        f"environments={args.with_environments}"
    )

    # --- resolve project list -----------------------------------------------
    if args.projects_csv:
        projects = load_projects_csv(args.projects_csv)
        roots = {
            p["path_with_namespace"].split("/", 1)[0]
            for p in projects
            if p["path_with_namespace"]
        }
        root_path = next(iter(roots)) if len(roots) == 1 else ""
        print(
            f"[info] Loaded {len(projects)} project(s) from "
            f"{args.projects_csv}"
        )
        if root_path:
            print(f"[info] Inferred root group: {root_path}")
        else:
            print(
                "[info] Multiple root groups in CSV — `group` / `subgroup` "
                "columns may be empty."
            )
    else:
        group_input: str | int = args.group
        if isinstance(group_input, str) and group_input.isdigit():
            group_input = int(group_input)
        print(f"[info] Resolving group: {group_input}")
        root = get_group(client, group_input)
        root_path = root["full_path"]
        print(f"[info] Root group: {root_path} (id={root['id']})")
        projects = list(
            list_group_projects(client, root["id"], archived="false")
        )
        print(
            f"[info] Found {len(projects)} project(s) "
            "(all nested subgroups, excluding archived)"
        )

    if not projects:
        print("[info] Nothing to do.")
        return 0

    # --- checkpoint / resume ------------------------------------------------
    checkpoint: CheckpointStore | None = None
    if not args.no_resume:
        checkpoint = CheckpointStore(args.checkpoint)
        if checkpoint.done_count:
            before = len(projects)
            projects = [
                p for p in projects
                if not checkpoint.is_done(int(p["id"]))
            ]
            print(
                f"[info] Resume: skipped {before - len(projects)} already-"
                f"processed project(s); {len(projects)} remaining "
                f"(checkpoint: {args.checkpoint})."
            )
        if not projects:
            print("[info] Everything already processed.")
            return 0

    # --- run ----------------------------------------------------------------
    sink = CsvSink(
        args.output,
        CSV_FIELDS,
        append=bool(checkpoint and checkpoint.done_count),
    )
    counts: dict = {
        "pipeline_run": 0,
        "ci_config": 0,
        "reusable_template": 0,
        "deployment": 0,
        "environment": 0,
    }
    counts_lock = threading.Lock()

    total = len(projects)
    done = 0
    failed = 0
    start = time.time()
    print(f"[info] Processing {total} project(s)...")

    try:
        with ThreadPoolExecutor(max_workers=max_workers) as ex:
            futures = {
                ex.submit(
                    process_project,
                    client, p, root_path,
                    sink, counts, counts_lock,
                    not args.no_runs,
                    not args.no_templates,
                    args.all_branches,
                    args.with_deployments,
                    args.with_environments,
                ): p
                for p in projects
            }
            for fut in as_completed(futures):
                project = futures[fut]
                pid = int(project["id"])
                try:
                    info = fut.result()
                except Exception as exc:  # noqa: BLE001
                    failed += 1
                    logging.error(
                        "[fail] project %s (id=%s): %s",
                        project.get("path_with_namespace"), pid, exc,
                    )
                    continue
                done += 1
                if checkpoint:
                    checkpoint.mark_done(pid)
                branches_note = (
                    f" branches={info['branches_scanned']}"
                    if args.all_branches else ""
                )
                print(
                    f"  [ok {done}/{total}] {info['full_path']}  "
                    f"ci={info['ci_status']} "
                    f"templates={info['templates']} "
                    f"runs={info['runs']} "
                    f"deploys={info['deployments']} "
                    f"envs={info['environments']}"
                    f"{branches_note}"
                )
                if done % 25 == 0 or done == total:
                    elapsed = time.time() - start
                    rate = done / max(elapsed, 1.0)
                    remaining = total - done
                    eta = remaining / max(rate, 0.001)
                    print(
                        f"[progress] {done}/{total} done  "
                        f"({rate:.2f} proj/s, elapsed {elapsed:.0f}s, "
                        f"eta ~{eta:.0f}s)"
                    )
    finally:
        sink.close()

    total_rows = sum(counts.values())
    print(f"[done] {total_rows} row(s) written to {args.output}")
    print(
        f"[stats] pipeline_run={counts['pipeline_run']} "
        f"ci_config={counts['ci_config']} "
        f"reusable_template={counts['reusable_template']} "
        f"deployment={counts['deployment']} "
        f"environment={counts['environment']}"
    )
    if failed:
        print(f"[stats] failed projects: {failed}", file=sys.stderr)
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
