"""CLI interface for GitLab-to-GitHub branch protection migration."""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

try:
    from dotenv import find_dotenv, load_dotenv
    _env = find_dotenv(usecwd=True)
    if _env:
        load_dotenv(_env, override=False)
except ImportError:
    pass

from .config.config_manager import load_config
from .gitlab_client import GitLabClient
from .github_client import GitHubClient
from .mapping_engine import MappingEngine
from .migration_engine import MigrationEngine, load_repo_mapping
from .validation_engine import ValidationEngine
from .report_generator import ReportGenerator

log = logging.getLogger("branch_protection")


def setup_logging(config: dict) -> None:
    """Configure logging from config."""
    log_config = config.get("logging", {})
    level = getattr(logging, log_config.get("level", "INFO").upper(), logging.INFO)
    fmt = log_config.get("format", "%(asctime)s [%(levelname)s] %(name)s: %(message)s")
    log_file = log_config.get("file")

    handlers: list[logging.Handler] = [logging.StreamHandler(sys.stdout)]
    if log_file:
        Path(log_file).parent.mkdir(parents=True, exist_ok=True)
        handlers.append(logging.FileHandler(log_file))

    logging.basicConfig(level=level, format=fmt, handlers=handlers)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="branch_protection_migrate",
        description="Migrate GitLab branch protection rules to GitHub.",
    )

    # Config
    parser.add_argument("--config", "-c", type=str, default=None,
                        help="Path to YAML config file (overrides defaults)")

    # Credentials
    parser.add_argument("--gitlab-url", type=str, help="GitLab instance URL")
    parser.add_argument("--gitlab-token", type=str, help="GitLab PAT")
    parser.add_argument("--github-token", type=str, help="GitHub PAT")
    parser.add_argument("--github-org", type=str, help="Target GitHub organization")

    # Input
    parser.add_argument("--repo-mapping", "-m", type=str, required=True,
                        help="Path to repository mapping file (CSV or JSON)")
    parser.add_argument("--inventory", "-i", type=str, default=None,
                        help="Path to GitLab branch protection inventory (CSV/JSON)")

    # Mode
    parser.add_argument("--dry-run", action="store_true",
                        help="Preview changes without applying")
    parser.add_argument("--validate-only", action="store_true",
                        help="Only validate existing protections, don't migrate")
    parser.add_argument("--migrate-only", action="store_true",
                        help="Migrate without post-validation")
    parser.add_argument("--resume", action="store_true",
                        help="Resume from last successful repo")
    parser.add_argument("--skip-existing", action="store_true",
                        help="Skip branches that already have protection")

    # Filters
    parser.add_argument("--include-repos", nargs="*", default=[],
                        help="Regex patterns for repos to include")
    parser.add_argument("--exclude-repos", nargs="*", default=[],
                        help="Regex patterns for repos to exclude")
    parser.add_argument("--include-branches", nargs="*", default=[],
                        help="Regex patterns for branches to include")
    parser.add_argument("--exclude-branches", nargs="*", default=[],
                        help="Regex patterns for branches to exclude")

    # Performance
    parser.add_argument("--workers", type=int, default=None,
                        help="Number of parallel workers")
    parser.add_argument("--batch-size", type=int, default=None,
                        help="Batch size for processing")

    # Output
    parser.add_argument("--output-dir", type=str, default=None,
                        help="Report output directory")
    parser.add_argument("--verbose", "-v", action="store_true",
                        help="Enable DEBUG logging")

    return parser


def apply_cli_overrides(config: dict, args: argparse.Namespace) -> dict:
    """Apply CLI arguments as overrides to config."""
    if args.gitlab_url:
        config.setdefault("gitlab", {})["url"] = args.gitlab_url
    if args.gitlab_token:
        config.setdefault("gitlab", {})["token"] = args.gitlab_token
    if args.github_token:
        config.setdefault("github", {})["token"] = args.github_token
    if args.github_org:
        config.setdefault("github", {})["org"] = args.github_org
    if args.dry_run:
        config.setdefault("migration", {})["dry_run"] = True
    if args.skip_existing:
        config.setdefault("migration", {})["skip_existing"] = True
    if args.include_repos:
        config.setdefault("migration", {})["include_repos"] = args.include_repos
    if args.exclude_repos:
        config.setdefault("migration", {})["exclude_repos"] = args.exclude_repos
    if args.include_branches:
        config.setdefault("migration", {})["include_branches"] = args.include_branches
    if args.exclude_branches:
        config.setdefault("migration", {})["exclude_branches"] = args.exclude_branches
    if args.workers:
        config.setdefault("migration", {})["parallel_workers"] = args.workers
    if args.batch_size:
        config.setdefault("migration", {})["batch_size"] = args.batch_size
    if args.output_dir:
        config.setdefault("reporting", {})["output_dir"] = args.output_dir
    if args.verbose:
        config.setdefault("logging", {})["level"] = "DEBUG"
    return config


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()

    # Load and merge config
    config = load_config(args.config)
    config = apply_cli_overrides(config, args)
    setup_logging(config)

    # Validate required fields
    gitlab_cfg = config.get("gitlab", {})
    github_cfg = config.get("github", {})

    if not gitlab_cfg.get("token") and not args.validate_only:
        log.error("GitLab token required. Set GITLAB_TOKEN env var or use --gitlab-token")
        return 1
    if not github_cfg.get("token"):
        log.error("GitHub token required. Set GITHUB_TOKEN env var or use --github-token")
        return 1
    if not github_cfg.get("org"):
        log.error("GitHub org required. Set GITHUB_ORG env var or use --github-org")
        return 1

    # Initialize clients
    gitlab = GitLabClient(
        base_url=gitlab_cfg.get("url", "https://gitlab.com"),
        token=gitlab_cfg.get("token", ""),
        per_page=gitlab_cfg.get("per_page", 100),
    )
    github = GitHubClient(
        token=github_cfg["token"],
        api_url=github_cfg.get("api_url", "https://api.github.com"),
    )
    mapping = MappingEngine(config)
    reporter = ReportGenerator(config)

    # Load repo mapping
    try:
        repo_mapping = load_repo_mapping(args.repo_mapping)
    except Exception as e:
        log.error(f"Failed to load repo mapping: {e}")
        return 1

    # Inject github_owner from config if not in mapping
    for m in repo_mapping:
        if "github_owner" not in m:
            m["github_owner"] = github_cfg["org"]

    log.info(f"GitLab Branch Protection Migration v1.0.0")
    log.info(f"Mode: {'validate-only' if args.validate_only else 'dry-run' if args.dry_run else 'live'}")
    log.info(f"Repositories: {len(repo_mapping)}")

    if args.validate_only:
        # Validation-only mode
        validator = ValidationEngine(config, github)
        validation_items = []
        for m in repo_mapping:
            owner = m.get("github_owner", github_cfg["org"])
            repo = m["github_repo"]
            # Get all protected branches from GitHub
            branches = m.get("branches", ["main"])
            for branch in branches:
                validation_items.append({
                    "github_owner": owner,
                    "github_repo": repo,
                    "branch": branch,
                    "github_payload": {},
                    "status": "success",
                })
        results = validator.validate_batch(validation_items)
        reporter.generate_validation_report(results)
        passed = sum(1 for r in results if r.status == "pass")
        log.info(f"Validation complete: {passed}/{len(results)} passed")
        return 0 if all(r.status == "pass" for r in results) else 1

    # Migration mode
    engine = MigrationEngine(config, gitlab, github, mapping)
    results = engine.run(repo_mapping, resume=args.resume)
    reporter.generate_migration_report(results)

    # Post-migration validation
    if not args.migrate_only and config.get("validation", {}).get("enabled", True):
        validator = ValidationEngine(config, github)
        validation_items = []
        for r in results:
            if r.status == "success":
                parts = r.github_repo.split("/", 1)
                owner = parts[0] if len(parts) > 1 else github_cfg["org"]
                repo = parts[-1]
                validation_items.append({
                    "github_owner": owner,
                    "github_repo": repo,
                    "branch": r.branch,
                    "github_payload": r.github_payload,
                    "status": r.status,
                })
        if validation_items:
            v_results = validator.validate_batch(validation_items)
            reporter.generate_validation_report(v_results)
            passed = sum(1 for v in v_results if v.status == "pass")
            log.info(f"Post-migration validation: {passed}/{len(v_results)} passed")

    # Summary
    success = sum(1 for r in results if r.status == "success")
    failed = sum(1 for r in results if r.status == "failed")
    skipped = sum(1 for r in results if r.status == "skipped")
    log.info(f"Migration complete: {success} success, {failed} failed, {skipped} skipped")

    return 1 if failed > 0 else 0


if __name__ == "__main__":
    sys.exit(main())
