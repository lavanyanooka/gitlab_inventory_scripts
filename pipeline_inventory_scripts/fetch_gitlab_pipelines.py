"""
Fetch a single-CSV inventory of EVERY pipeline asset under a GitLab group:

  * Pipeline runs  — every actual run on every branch/tag, every status.
  * CI config     — each project's `.gitlab-ci.yml` (or custom ci_config_path).
  * Reusable pipelines — any YAML files stored in `templates/`, `ci-templates/`
                         or `ci/` folders of projects (typical home of
                         group/subgroup-level reusable pipelines that other
                         projects `include:` via `project: ... file: ...`).

Recurses through every nested subgroup at any depth.

Output:
  ./output/pipelines.csv with columns:
    type, pipeline_id, group, subgroup, project, branch, status, source,
    sha, file_path, file_ref, created_at, updated_at, web_url,
    project_web_url, project_id

Usage:
    set GITLAB_TOKEN=glpat-xxxxxxxxxxxx
    python fetch_gitlab_pipelines.py --group migration-github1
    python fetch_gitlab_pipelines.py --group my-org --output all.csv
    python fetch_gitlab_pipelines.py --group my-org --no-runs        # skip pipeline runs
    python fetch_gitlab_pipelines.py --group my-org --no-templates   # skip YAML scan
"""

from __future__ import annotations

import argparse
import csv
import os
import sys
import time
from pathlib import Path
from typing import Iterable
from urllib.parse import quote

import requests


DEFAULT_GITLAB_URL = "https://gitlab.com"
PER_PAGE = 100
DEFAULT_CI_FILE = ".gitlab-ci.yml"
TEMPLATE_DIRS = ("templates", "ci-templates", "ci")
YAML_EXTS = (".yml", ".yaml")


class GitLabClient:
    def __init__(self, base_url: str, token: str, timeout: int = 30):
        self.base_url = base_url.rstrip("/")
        self.api = f"{self.base_url}/api/v4"
        self.session = requests.Session()
        self.session.headers.update({"PRIVATE-TOKEN": token})
        self.timeout = timeout

    def _get(self, path: str, params: dict | None = None) -> requests.Response:
        url = f"{self.api}{path}"
        for _ in range(5):
            resp = self.session.get(url, params=params, timeout=self.timeout)
            if resp.status_code == 429:
                time.sleep(int(resp.headers.get("Retry-After", "5")))
                continue
            return resp
        return resp  # type: ignore[return-value]

    def paginated(self, path: str, params: dict | None = None) -> Iterable[dict]:
        params = dict(params or {})
        params.setdefault("per_page", PER_PAGE)
        page = 1
        while True:
            params["page"] = page
            resp = self._get(path, params=params)
            if resp.status_code != 200:
                raise RuntimeError(
                    f"GET {path} failed [{resp.status_code}]: {resp.text[:300]}"
                )
            batch = resp.json()
            if not batch:
                return
            for item in batch:
                yield item
            next_page = resp.headers.get("X-Next-Page")
            if not next_page:
                return
            page = int(next_page)

    def get_group(self, group: str | int) -> dict:
        ident = group if isinstance(group, int) else quote(str(group), safe="")
        resp = self._get(f"/groups/{ident}")
        if resp.status_code != 200:
            raise RuntimeError(
                f"Could not load group '{group}' [{resp.status_code}]: {resp.text[:300]}"
            )
        return resp.json()

    def list_group_projects(self, group_id: int) -> Iterable[dict]:
        yield from self.paginated(
            f"/groups/{group_id}/projects",
            params={
                "include_subgroups": "true",
                "archived": "false",
                "with_shared": "false",
            },
        )

    def list_all_pipelines(self, project_id: int) -> Iterable[dict]:
        yield from self.paginated(
            f"/projects/{project_id}/pipelines",
            params={"order_by": "id", "sort": "desc"},
        )

    def file_exists(self, project_id: int, file_path: str, ref: str) -> bool:
        encoded = quote(file_path, safe="")
        # HEAD doesn't include file content -> cheap existence check.
        url = f"{self.api}/projects/{project_id}/repository/files/{encoded}"
        resp = self.session.head(url, params={"ref": ref}, timeout=self.timeout)
        return resp.status_code == 200

    def list_tree(self, project_id: int, path: str, ref: str) -> list[dict]:
        try:
            return list(self.paginated(
                f"/projects/{project_id}/repository/tree",
                params={"path": path, "ref": ref, "recursive": "true"},
            ))
        except RuntimeError:
            return []


CSV_FIELDS = [
    "type",            # pipeline_run | ci_config | reusable_template
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
]


def split_namespace(path_with_namespace: str, root_group: str) -> tuple[str, str, str]:
    """Split `root/sub1/sub2/project` -> (root, 'sub1/sub2', 'project')."""
    parts = path_with_namespace.split("/")
    project = parts[-1]
    namespace_parts = parts[:-1]
    root_parts = root_group.split("/")
    if namespace_parts[: len(root_parts)] == root_parts:
        subgroup_parts = namespace_parts[len(root_parts):]
    else:
        root_parts = namespace_parts[:1]
        subgroup_parts = namespace_parts[1:]
    return "/".join(root_parts), "/".join(subgroup_parts), project


def parse_ci_config_path(raw: str | None) -> tuple[str, str | None]:
    """`ci_config_path` may be `path/file.yml@group/project:ref`."""
    if not raw:
        return DEFAULT_CI_FILE, None
    path, _, external = raw.partition("@")
    return (path or DEFAULT_CI_FILE), (external or None)


def discover_reusable_yaml(client: GitLabClient, project: dict) -> list[dict]:
    """Find pipeline-like YAML files under templates/, ci-templates/, ci/."""
    ref = project.get("default_branch")
    if not ref:
        return []
    found: list[dict] = []
    for top in TEMPLATE_DIRS:
        for node in client.list_tree(project["id"], top, ref):
            if node.get("type") == "blob" and node.get("path", "").lower().endswith(YAML_EXTS):
                found.append(node)
    return found


def file_web_url(project_web_url: str, ref: str, path: str) -> str:
    return f"{project_web_url}/-/blob/{ref}/{path}"


def collect(client: GitLabClient,
            root_group: str | int,
            output_csv: Path,
            include_runs: bool,
            include_templates: bool) -> dict[str, int]:
    print(f"[info] Resolving group: {root_group}")
    root = client.get_group(root_group)
    root_id = root["id"]
    root_path = root["full_path"]
    print(f"[info] Root group: {root_path} (id={root_id})")

    projects = list(client.list_group_projects(root_id))
    print(f"[info] Found {len(projects)} project(s) under group (all nested subgroups)")

    output_csv.parent.mkdir(parents=True, exist_ok=True)
    counts = {"pipeline_run": 0, "ci_config": 0, "reusable_template": 0}

    with output_csv.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=CSV_FIELDS)
        writer.writeheader()

        for idx, project in enumerate(projects, 1):
            full_path = project["path_with_namespace"]
            group_name, subgroup, project_name = split_namespace(full_path, root_path)
            project_web = project.get("web_url", "")
            default_branch = project.get("default_branch") or ""

            base_row = {
                "group": group_name,
                "subgroup": subgroup,
                "project": project_name,
                "project_web_url": project_web,
                "project_id": project["id"],
            }

            # --- 1. Project CI config (.gitlab-ci.yml or custom path) -----------
            ci_path, external_ref = parse_ci_config_path(project.get("ci_config_path"))
            ci_status = "external" if external_ref else "missing"
            if not external_ref and default_branch:
                try:
                    if client.file_exists(project["id"], ci_path, default_branch):
                        ci_status = "found"
                except Exception as exc:  # noqa: BLE001
                    ci_status = f"error: {exc}"

            if ci_status in ("found", "external"):
                counts["ci_config"] += 1
                writer.writerow({
                    **base_row,
                    "type": "ci_config",
                    "pipeline_id": "",
                    "branch": default_branch,
                    "status": ci_status,
                    "source": "",
                    "sha": "",
                    "file_path": ci_path,
                    "file_ref": external_ref or default_branch,
                    "created_at": "",
                    "updated_at": "",
                    "web_url": "" if external_ref else file_web_url(project_web, default_branch, ci_path),
                })

            # --- 2. Reusable pipeline YAML files (templates/, ci-templates/, ci/)
            templates: list[dict] = []
            if include_templates:
                try:
                    templates = discover_reusable_yaml(client, project)
                except Exception as exc:  # noqa: BLE001
                    print(f"   [warn] template scan failed for {full_path}: {exc}")
                for node in templates:
                    counts["reusable_template"] += 1
                    writer.writerow({
                        **base_row,
                        "type": "reusable_template",
                        "pipeline_id": "",
                        "branch": default_branch,
                        "status": "",
                        "source": "",
                        "sha": "",
                        "file_path": node.get("path", ""),
                        "file_ref": default_branch,
                        "created_at": "",
                        "updated_at": "",
                        "web_url": file_web_url(project_web, default_branch, node.get("path", "")),
                    })

            # --- 3. Pipeline runs (every branch, every status) ------------------
            run_count = 0
            if include_runs:
                try:
                    for p in client.list_all_pipelines(project["id"]):
                        run_count += 1
                        counts["pipeline_run"] += 1
                        writer.writerow({
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
                        })
                except RuntimeError as exc:
                    print(f"   [warn] pipelines fetch failed for {full_path}: {exc}")

            print(f"[{idx}/{len(projects)}] {full_path}  "
                  f"ci={ci_status} templates={len(templates)} runs={run_count}")

    total = sum(counts.values())
    print(f"[done] {total} row(s) written to {output_csv}")
    print(f"[stats] pipeline_run={counts['pipeline_run']} "
          f"ci_config={counts['ci_config']} "
          f"reusable_template={counts['reusable_template']}")
    return counts


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--group", required=True,
                        help="Root group full path (e.g. 'migration-github1') or numeric ID.")
    parser.add_argument("--gitlab-url", default=os.environ.get("GITLAB_URL", DEFAULT_GITLAB_URL),
                        help="GitLab base URL (default: env GITLAB_URL or https://gitlab.com).")
    parser.add_argument("--token", default=os.environ.get("GITLAB_TOKEN"),
                        help="Personal access token with read_api scope (default: env GITLAB_TOKEN).")
    parser.add_argument("--output", default="./output/pipelines.csv",
                        help="Output CSV path (default: ./output/pipelines.csv).")
    parser.add_argument("--no-runs", action="store_true",
                        help="Skip listing pipeline runs (only inventory CI configs + templates).")
    parser.add_argument("--no-templates", action="store_true",
                        help="Skip scanning repos for reusable pipeline YAML.")
    args = parser.parse_args(argv)

    if not args.token:
        print("error: GITLAB_TOKEN env var or --token is required.", file=sys.stderr)
        return 2

    group: str | int = args.group
    if group.isdigit():
        group = int(group)

    client = GitLabClient(args.gitlab_url, args.token)
    try:
        collect(client, group, Path(args.output),
                include_runs=not args.no_runs,
                include_templates=not args.no_templates)
    except RuntimeError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
